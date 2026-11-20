package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	rpcxray "github.com/SolidRPC/rpc-xray/go"
	"github.com/ethereum/go-ethereum/ethclient"
	"github.com/ethereum/go-ethereum/rpc"
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
	defer listener.Close()
	telemetry := rpcxray.New(rpcxray.Options{})
	httpClient := &http.Client{
		Transport: &rpcxray.Transport{Xray: telemetry, Chain: chain, Base: http.DefaultTransport},
		Timeout:   4500 * time.Millisecond,
	}
	defer httpClient.CloseIdleConnections()
	rpcClient, err := rpc.DialOptions(ctx, rpcURL, rpc.WithHTTPClient(httpClient))
	if err != nil {
		return fmt.Errorf("cannot initialize the RPC client")
	}
	client := ethclient.NewClient(rpcClient)
	defer client.Close()
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
	fmt.Printf("go-ethereum example listening on %s. Metrics at /metrics. Press Ctrl+C to stop.\n", listener.Addr())
	pollCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		for pollCtx.Err() == nil {
			started := time.Now()
			callCtx, cancelCall := context.WithTimeout(pollCtx, 4500*time.Millisecond)
			head, callErr := client.BlockNumber(callCtx)
			cancelCall()
			mu.Lock()
			if callErr == nil {
				state["status"] = "ok"
				state["blockNumber"] = fmt.Sprintf("0x%x", head)
			} else {
				state["status"] = "error"
			}
			mu.Unlock()
			delay := time.NewTimer(max(0, time.Second-time.Since(started)))
			select {
			case <-pollCtx.Done():
				delay.Stop()
				return
			case <-delay.C:
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
