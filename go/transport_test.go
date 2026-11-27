package rpcxray

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type failingBody struct{ err error }

func (b failingBody) Read([]byte) (int, error) { return 0, b.err }
func (b failingBody) Close() error             { return nil }

func TestCancellationFailureClassification(t *testing.T) {
	for _, phase := range []string{"headers", "body"} {
		for _, mode := range []string{"legacy_cancel", "context_cancel", "expired_deadline", "future_deadline_network", "open_cancel_network", "network"} {
			t.Run(phase+"/"+mode, func(t *testing.T) {
				ctx := context.Background()
				var cancelChannel chan struct{}
				outcome := "timeout"
				switch mode {
				case "legacy_cancel":
					cancelChannel = make(chan struct{})
					close(cancelChannel)
				case "context_cancel":
					var cancel context.CancelFunc
					ctx, cancel = context.WithCancel(ctx)
					cancel()
				case "expired_deadline":
					var cancel context.CancelFunc
					ctx, cancel = context.WithDeadline(ctx, time.Now().Add(-time.Second))
					defer cancel()
				case "future_deadline_network":
					var cancel context.CancelFunc
					ctx, cancel = context.WithTimeout(ctx, time.Minute)
					defer cancel()
					outcome = "network"
				case "open_cancel_network":
					cancelChannel = make(chan struct{})
					outcome = "network"
				default:
					outcome = "network"
				}
				original := errors.New("transport failure")
				if mode == "legacy_cancel" {
					original = errors.New("net/http: request canceled")
				}
				x := New(Options{})
				transport := &Transport{Xray: x, Chain: "ethereum", Base: roundTripFunc(func(*http.Request) (*http.Response, error) {
					if phase == "headers" {
						return nil, original
					}
					return &http.Response{StatusCode: 200, Body: failingBody{original}, ContentLength: -1}, nil
				})}
				request, _ := http.NewRequestWithContext(ctx, "POST", "https://fixture.example", strings.NewReader(requestBody))
				request.Cancel = cancelChannel
				response, err := transport.RoundTrip(request)
				if phase == "body" {
					if err != nil {
						t.Fatal(err)
					}
					_, err = io.ReadAll(response.Body)
					_ = response.Body.Close()
				}
				if err != original {
					t.Fatal("original error identity changed")
				}
				if !strings.Contains(x.Metrics(), `outcome="`+outcome+`"} 1`) {
					t.Fatal(x.Metrics())
				}
			})
		}
	}
}

func TestHTTPClientLegacyTimeout(t *testing.T) {
	original := errors.New("net/http: request canceled")
	x := New(Options{})
	client := &http.Client{Timeout: 20 * time.Millisecond, Transport: &Transport{Xray: x, Base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		select {
		case <-r.Cancel:
			return nil, original
		case <-time.After(time.Second):
			return nil, errors.New("client did not cancel the transport")
		}
	})}}
	_, err := client.Post("https://fixture.example", "application/json", strings.NewReader(requestBody))
	var timeout net.Error
	if !errors.As(err, &timeout) || !timeout.Timeout() {
		t.Fatalf("caller error changed: %v", err)
	}
	if !strings.Contains(x.Metrics(), `outcome="timeout"} 1`) {
		t.Fatal(x.Metrics())
	}
}

func TestHTTPClientTimeoutDuringResponseBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0",`)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	x := New(Options{})
	client := &http.Client{Timeout: 30 * time.Millisecond, Transport: &Transport{Xray: x}}
	defer client.CloseIdleConnections()
	response, err := client.Post(server.URL, "application/json", strings.NewReader(requestBody))
	if err != nil {
		t.Fatal(err)
	}
	_, err = io.ReadAll(response.Body)
	_ = response.Body.Close()
	var timeout net.Error
	if !errors.As(err, &timeout) || !timeout.Timeout() {
		t.Fatalf("caller did not receive a timeout: %v", err)
	}
	if !strings.Contains(x.Metrics(), `outcome="timeout"} 1`) {
		t.Fatal(x.Metrics())
	}
}

func TestCloseBeforeEOF(t *testing.T) {
	const fault = `{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"failure"}}`
	for _, tc := range []struct {
		name, consumed string
		length         int64
		outcome        string
	}{
		{"chunked fault", fault, -1, "rpc_error"},
		{"trailing whitespace", fault + " \n", -1, "rpc_error"},
		{"partial object", fault[:12], -1, "ok"},
		{"unknown scalar", "42", -1, "ok"},
		{"unread", "", -1, "ok"},
		{"incomplete known length", fault, int64(len(fault) + 6), "ok"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			x := New(Options{})
			body := io.MultiReader(strings.NewReader(tc.consumed), unexpectedRead{t})
			transport := Transport{Xray: x, Base: roundTripFunc(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, ContentLength: tc.length, Body: io.NopCloser(body)}, nil
			})}
			req, _ := http.NewRequest("POST", "http://fixture.test", strings.NewReader(requestBody))
			res, err := transport.RoundTrip(req)
			if err != nil {
				t.Fatal(err)
			}
			if tc.name == "chunked fault" {
				var payload struct{ Error struct{ Code int } }
				if err := json.NewDecoder(res.Body).Decode(&payload); err != nil || payload.Error.Code != -32603 {
					t.Fatal(payload, err)
				}
			} else if _, err := io.ReadFull(res.Body, make([]byte, len(tc.consumed))); err != nil {
				t.Fatal(err)
			}
			for i := 0; i < 2; i++ {
				if err := res.Body.Close(); err != nil {
					t.Fatal(err)
				}
			}
			if !strings.Contains(x.Metrics(), `outcome="`+tc.outcome+`"} 1`) {
				t.Fatal(x.Metrics())
			}
		})
	}
}

type unexpectedRead struct{ t *testing.T }

func (r unexpectedRead) Read([]byte) (int, error) {
	r.t.Fatal("telemetry read beyond the consumer")
	return 0, io.EOF
}
