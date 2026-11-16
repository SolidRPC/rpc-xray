package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	rpcxray "github.com/SolidRPC/rpc-xray/go"
)

func run(ctx context.Context) error {
	rpcURL, chain := os.Getenv("RPC_URL"), os.Getenv("CHAIN")
	u, err := url.Parse(rpcURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || chain == "" {
		return fmt.Errorf("set RPC_URL to an HTTP endpoint and CHAIN to its label")
	}
	host, port := os.Getenv("HOST"), os.Getenv("PORT")
	if host == "" {
		host = "127.0.0.1"
	}
	if port == "" {
		port = "9464"
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(host, port))
	if err != nil {
		return fmt.Errorf("cannot bind metrics server")
	}
	telemetry := rpcxray.New(rpcxray.Options{})
	client := &http.Client{Transport: &rpcxray.Transport{Xray: telemetry, Chain: chain}, Timeout: 4500 * time.Millisecond}
	defer client.CloseIdleConnections()
	state := map[string]any{"chain": chain, "status": "starting", "blockNumber": nil}
	var mu sync.Mutex
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", telemetry.Handler())
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		mu.Lock()
		defer mu.Unlock()
		_ = json.NewEncoder(w).Encode(state)
	})
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	fmt.Printf("RPC example listening on %s. Metrics at /metrics. Press Ctrl+C to stop.\n", listener.Addr())
	pollCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for id := 1; ; id++ {
			if pollCtx.Err() != nil {
				return
			}
			body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": "eth_blockNumber", "params": []any{}})
			request, _ := http.NewRequestWithContext(pollCtx, http.MethodPost, rpcURL, bytes.NewReader(body))
			request.Header.Set("Content-Type", "application/json")
			response, callErr := client.Do(request)
			result := ""
			if callErr == nil {
				var payload struct {
					Result string          `json:"result"`
					Error  json.RawMessage `json:"error"`
				}
				responseBody, readErr := io.ReadAll(response.Body)
				decodeErr := json.Unmarshal(responseBody, &payload)
				_ = response.Body.Close()
				if readErr == nil && decodeErr == nil && response.StatusCode < 400 && len(payload.Error) == 0 && len(payload.Result) > 2 && payload.Result[:2] == "0x" {
					if _, err := strconv.ParseUint(payload.Result[2:], 16, 64); err == nil {
						result = payload.Result
					}
				}
			}
			mu.Lock()
			if result != "" {
				state["status"] = "ok"
				state["blockNumber"] = result
			} else {
				state["status"] = "error"
			}
			mu.Unlock()
			select {
			case <-pollCtx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	select {
	case <-ctx.Done():
	case err = <-serveErr:
	}
	cancel()
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelShutdown()
	_ = server.Shutdown(shutdown)
	<-done
	if err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("metrics server stopped")
	}
	return nil
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
