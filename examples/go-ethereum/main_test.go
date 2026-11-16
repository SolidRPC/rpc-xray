package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	rpcxray "github.com/SolidRPC/rpc-xray/go"
	"github.com/ethereum/go-ethereum/ethclient"
	"github.com/ethereum/go-ethereum/rpc"
)

func TestEthclientAndMixedBatch(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("X-Existing-Header") != "preserved" {
			t.Error("existing client header changed")
		}
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		if body[0] == '[' {
			var requests []struct{ ID json.RawMessage }
			_ = json.Unmarshal(body, &requests)
			_ = json.NewEncoder(w).Encode([]map[string]any{
				{"jsonrpc": "2.0", "id": requests[2].ID, "error": map[string]any{"code": -32005, "message": "rate limit exceeded"}},
				{"jsonrpc": "2.0", "id": requests[0].ID, "result": "0x10"},
				{"jsonrpc": "2.0", "id": requests[1].ID, "error": map[string]any{"code": 3, "message": "execution reverted: private-message"}},
			})
			return
		}
		var request struct{ ID json.RawMessage }
		_ = json.Unmarshal(body, &request)
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": request.ID, "result": "0x10"})
	}))
	defer server.Close()
	telemetry := rpcxray.New(rpcxray.Options{})
	httpClient := &http.Client{Transport: &rpcxray.Transport{Xray: telemetry, Chain: "ethereum"}, Timeout: time.Second}
	defer httpClient.CloseIdleConnections()
	rpcClient, err := rpc.DialOptions(context.Background(), server.URL+"/private-path?key=private-query", rpc.WithHTTPClient(httpClient))
	if err != nil {
		t.Fatal(err)
	}
	defer rpcClient.Close()
	rpcClient.SetHeader("X-Existing-Header", "preserved")
	head, err := ethclient.NewClient(rpcClient).BlockNumber(context.Background())
	if err != nil || head != 16 {
		t.Fatalf("result changed: %d %v", head, err)
	}
	var first, second, third string
	batch := []rpc.BatchElem{
		{Method: "eth_blockNumber", Result: &first},
		{Method: "eth_call", Result: &second},
		{Method: "eth_getBalance", Result: &third},
	}
	if err := rpcClient.BatchCallContext(context.Background(), batch); err != nil {
		t.Fatal(err)
	}
	var revert, rateLimit rpc.Error
	if first != "0x10" || batch[0].Error != nil || !errors.As(batch[1].Error, &revert) || revert.ErrorCode() != 3 || !errors.As(batch[2].Error, &rateLimit) || rateLimit.ErrorCode() != -32005 {
		t.Fatal("batch result or error identity changed")
	}
	for i := 0; i < 5; i++ {
		recorder := httptest.NewRecorder()
		telemetry.Handler().ServeHTTP(recorder, httptest.NewRequest("GET", "/metrics", nil))
		if recorder.Code != 200 || recorder.Header().Get("Content-Type") != rpcxray.ContentType {
			t.Fatal("bad metrics response")
		}
		for _, expected := range []string{`method="eth_blockNumber",outcome="ok"} 2`, `method="eth_call",outcome="caller_error"} 1`, `method="eth_getBalance",outcome="rate_limited"} 1`, `method="batch"} 1`} {
			if !strings.Contains(recorder.Body.String(), expected) {
				t.Fatalf("missing %s", expected)
			}
		}
		if strings.Contains(recorder.Body.String(), "private-") {
			t.Fatal("private content exported")
		}
	}
	if calls.Load() != 2 {
		t.Fatalf("unexpected provider traffic: %d", calls.Load())
	}
}

func TestEthclientHTTPTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		<-r.Context().Done()
	}))
	defer server.Close()
	telemetry := rpcxray.New(rpcxray.Options{})
	httpClient := &http.Client{Transport: &rpcxray.Transport{Xray: telemetry, Chain: "ethereum"}, Timeout: 20 * time.Millisecond}
	defer httpClient.CloseIdleConnections()
	rpcClient, err := rpc.DialOptions(context.Background(), server.URL, rpc.WithHTTPClient(httpClient))
	if err != nil {
		t.Fatal(err)
	}
	defer rpcClient.Close()
	client := ethclient.NewClient(rpcClient)
	for i := 0; i < 6; i++ {
		_, err := client.BlockNumber(context.Background())
		var timeout net.Error
		if !errors.As(err, &timeout) || !timeout.Timeout() {
			t.Fatalf("timeout lost: %v", err)
		}
	}
	if !strings.Contains(telemetry.Metrics(), `outcome="timeout"} 6`) || strings.Contains(telemetry.Metrics(), `outcome="network"`) {
		t.Fatal(telemetry.Metrics())
	}
}

func TestRunShutdownCancelsActiveRequest(t *testing.T) {
	requestStarted, requestCanceled := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		close(requestStarted)
		<-r.Context().Done()
		close(requestCanceled)
	}))
	defer server.Close()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	t.Setenv("RPC_URL", server.URL)
	t.Setenv("CHAIN", "ethereum")
	t.Setenv("HOST", "127.0.0.1")
	t.Setenv("PORT", strconv.Itoa(port))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- run(ctx) }()
	select {
	case <-requestStarted:
	case <-time.After(3 * time.Second):
		t.Fatal("poll did not start")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("shutdown did not cancel the request")
	}
	select {
	case <-requestCanceled:
	case <-time.After(time.Second):
		t.Fatal("provider request still active")
	}
	connection, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), time.Second)
	if err == nil {
		_ = connection.Close()
		t.Fatal("metrics listener still open")
	}
}
