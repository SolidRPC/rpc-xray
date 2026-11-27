package rpcxray

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const requestBody = `{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}`
const successBody = `{"jsonrpc":"2.0","id":1,"result":"0x10"}`

func TestSharedVectors(t *testing.T) {
	body, err := os.ReadFile("testdata/vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors []struct {
		Name, Request, Failure string
		Status                 int
		Response               *string
		Expected               []Entry
	}
	if err = json.Unmarshal(body, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			var response []byte
			if v.Response != nil {
				response = []byte(*v.Response)
			}
			if got := classify(parseRequest([]byte(v.Request)), v.Status, response, v.Failure); !reflect.DeepEqual(got, v.Expected) {
				t.Fatalf("got %v want %v", got, v.Expected)
			}
		})
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPassThroughBatchAndPrivacy(t *testing.T) {
	x := New(Options{})
	batch := `[{"id":1,"method":"eth_blockNumber"},{"id":2,"method":"eth_call"}]`
	body := `[{"id":2,"error":{"code":3,"message":"revert"}},{"id":1,"result":null}]`
	var calls int
	transport := &Transport{Xray: x, Chain: "base", Base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		got, _ := io.ReadAll(r.Body)
		if string(got) != batch || r.Header.Get("X-Fixture") != "test" {
			t.Error("request changed")
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"X-Response": []string{"unchanged"}}, Body: io.NopCloser(strings.NewReader(body)), ContentLength: -1}, nil
	})}
	r, _ := http.NewRequest("POST", "https://name:secret@RPC.EXAMPLE:8545/private?key=secret", strings.NewReader(batch))
	r.Header.Set("X-Fixture", "test")
	response, err := transport.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if string(got) != body || response.Header.Get("X-Response") != "unchanged" || calls != 1 {
		t.Fatal("response changed")
	}
	metrics := x.Metrics()
	for _, private := range []string{"secret", "private", "name:", "key="} {
		if strings.Contains(metrics, private) {
			t.Fatal("credentials in metrics")
		}
	}
	for _, expected := range []string{`provider="rpc.example:8545"`, `method="eth_call",outcome="caller_error"} 1`, `method="eth_blockNumber",outcome="ok"} 1`, `method="batch"} 1`} {
		if !strings.Contains(metrics, expected) {
			t.Fatalf("missing %s in %s", expected, metrics)
		}
	}
}

func TestErrorsAndNonReplayableRequests(t *testing.T) {
	for _, tc := range []struct {
		err     error
		outcome string
	}{{context.DeadlineExceeded, "timeout"}, {context.Canceled, "timeout"}, {errors.New("offline"), "network"}} {
		x := New(Options{})
		transport := &Transport{Xray: x, Base: roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, tc.err })}
		r, _ := http.NewRequest("POST", "https://rpc.example", strings.NewReader(requestBody))
		_, err := transport.RoundTrip(r)
		if err != tc.err {
			t.Fatal("error changed")
		}
		if !strings.Contains(x.Metrics(), `outcome="`+tc.outcome+`"} 1`) {
			t.Fatal(x.Metrics())
		}
	}
	x := New(Options{})
	r, _ := http.NewRequest("POST", "https://rpc.example", io.NopCloser(strings.NewReader(requestBody)))
	transport := &Transport{Xray: x, Base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		got, _ := io.ReadAll(r.Body)
		if string(got) != requestBody {
			t.Fatal("stream consumed")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(successBody))}, nil
	})}
	response, _ := transport.RoundTrip(r)
	_ = response.Body.Close()
	if strings.Contains(x.Metrics(), "rpc_xray_requests_total") {
		t.Fatal("stream recorded")
	}
}

func TestRealHTTPAndConcurrentScrapes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, successBody)
	}))
	defer server.Close()
	x := New(Options{})
	client := &http.Client{Transport: &Transport{Xray: x, Chain: "ethereum"}, Timeout: time.Second}
	defer client.CloseIdleConnections()
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			response, err := client.Post(server.URL, "application/json", strings.NewReader(requestBody))
			if err != nil {
				t.Error(err)
				return
			}
			body, err := io.ReadAll(response.Body)
			_ = response.Body.Close()
			if err != nil || string(body) != successBody {
				t.Error("bad response")
			}
			_ = x.Metrics()
		}()
	}
	wg.Wait()
	if !strings.Contains(x.Metrics(), `outcome="ok"} 20`) {
		t.Fatal(x.Metrics())
	}
	w := httptest.NewRecorder()
	x.Handler().ServeHTTP(w, httptest.NewRequest("GET", "/metrics", nil))
	if w.Header().Get("Content-Type") != ContentType {
		t.Fatal("wrong content type")
	}
}

func TestCapProbesAndLimits(t *testing.T) {
	x := New(Options{MaxBodyBytes: 100})
	transport := &Transport{Xray: x, Base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(bytes.Repeat([]byte("x"), 200))), ContentLength: -1}, nil
	})}
	r, _ := http.NewRequest("POST", "https://rpc.example", strings.NewReader(requestBody))
	response, _ := transport.RoundTrip(r)
	body, _ := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if len(body) != 200 {
		t.Fatal("truncated body")
	}
	if !strings.Contains(x.Metrics(), `outcome="ok"} 1`) {
		t.Fatal(x.Metrics())
	}
	head := uint64(10)
	x.RecordProbe("https://rpc.example", "ethereum", &head)
	head = 12
	x.RecordProbe("https://second.example", "ethereum", &head)
	x.RecordProbe("https://rpc.example", "ethereum", nil)
	if !strings.Contains(x.Metrics(), `rpc_xray_head_lag_blocks{chain="ethereum",provider="rpc.example"} 2`) {
		t.Fatal(x.Metrics())
	}
	for i := 0; i < 40; i++ {
		x.Start("https://rpc.example/"+strings.Repeat("x", i), "ethereum", []byte(requestBody)).Finish(200, []byte(successBody), "")
	}
	if len(x.endpoints) != 32 || !strings.Contains(x.Metrics(), `provider="other"`) {
		t.Fatal("endpoint cap failed")
	}
	if ChainLabel(1) != "ethereum" || ChainLabel(123456) != "eip155:123456" {
		t.Fatal("chain mapping")
	}
}

func TestMetricContract(t *testing.T) {
	value, revenue := 0.1, 100.0
	x := New(Options{ValuePerRequest: &value, RevenuePerHour: &revenue})
	o := x.Start("https://rpc.example", "ethereum", []byte(requestBody))
	o.started = time.Now().Add(-1500 * time.Millisecond)
	o.Finish(200, []byte(successBody), "")
	head := uint64(10)
	x.RecordProbe("https://rpc.example", "ethereum", &head)
	if path := os.Getenv("RPC_XRAY_METRICS_FIXTURE"); path != "" {
		if err := os.WriteFile(path, []byte(x.Metrics()), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if strings.Count(x.Metrics(), "# TYPE ") != 12 {
		t.Fatal(x.Metrics())
	}
}

func gaugeLine(x *Xray, name, provider string) string {
	prefix := name + `{chain="ethereum",provider="` + provider + `"} `
	for _, line := range strings.Split(x.Metrics(), "\n") {
		if strings.HasPrefix(line, prefix) {
			return strings.TrimPrefix(line, prefix)
		}
	}
	return ""
}

func TestPassiveHeadFromBlockNumberAnswers(t *testing.T) {
	x := New(Options{})
	x.Start("https://rpc.example", "ethereum", []byte(`{"id":1,"method":"eth_blockNumber"}`)).Finish(200, []byte(`{"id":1,"result":"0x10"}`), "")
	if got := gaugeLine(x, "rpc_xray_head_block", "rpc.example"); got != "16" {
		t.Fatal(got, x.Metrics())
	}
	batch := []byte(`[{"id":"a","method":"eth_call"},{"id":"b","method":"eth_blockNumber"}]`)
	for _, bad := range []string{`{"id":"a","result":"0xffff"}`, `{"result":"0xffff"}`, `{"id":"b","result":"0xffff"}junk`} {
		x.Start("https://rpc.example", "ethereum", batch).Finish(200, []byte(bad), "")
		if got := gaugeLine(x, "rpc_xray_head_block", "rpc.example"); got != "16" {
			t.Fatal(got)
		}
	}
	x.Start("https://rpc.example", "ethereum", batch).Finish(200, []byte(`[{"id":"b","result":"0x20"},{"id":"a","result":"0x99"}]`), "")
	for _, body := range []string{`{"id":1,"result":"0x1"}`, `{"id":1,"result":"latest"}`, `{"id":1,"error":{"code":-32000,"message":"x"}}`} {
		x.Start("https://rpc.example", "ethereum", []byte(`{"id":1,"method":"eth_blockNumber"}`)).Finish(200, []byte(body), "")
	}
	x.Start("https://rpc.example", "ethereum", []byte(`{"id":1,"method":"eth_blockNumber"}`)).Finish(500, []byte(`{"id":1,"result":"0x99"}`), "")
	if got := gaugeLine(x, "rpc_xray_head_block", "rpc.example"); got != "1" {
		t.Fatal(got)
	}
	x.Start("https://other.example", "ethereum", []byte(`{"id":1,"method":"eth_call"}`)).Finish(200, []byte(`{"id":1,"result":"0x99"}`), "")
	if gaugeLine(x, "rpc_xray_head_block", "other.example") != "" || strings.Contains(x.Metrics(), "rpc_xray_probe_requests_total") {
		t.Fatal(x.Metrics())
	}
}

func TestPassiveHeadExpiresWhenAppStopsPolling(t *testing.T) {
	x := New(Options{})
	finish := func() {
		x.Start("https://rpc.example", "ethereum", []byte(requestBody)).Finish(200, []byte(successBody), "")
	}
	finish()
	for base, state := range x.heads {
		state.advanced = state.advanced.Add(-600 * time.Second)
		state.seen = state.seen.Add(-600 * time.Second)
		x.heads[base] = state
	}
	if gaugeLine(x, "rpc_xray_head_age_seconds", "rpc.example") != "" {
		t.Fatal(x.Metrics())
	}
	finish()
	if gaugeLine(x, "rpc_xray_head_age_seconds", "rpc.example") == "" || !x.staleSeen {
		t.Fatal(x.Metrics())
	}
}

func TestOfferEnabledByDefaultAndLoggedOnce(t *testing.T) {
	lines := []string{}
	x := New(Options{Logger: func(line string) { lines = append(lines, line) }})
	if !strings.Contains(x.Metrics(), "rpc_xray_offers_enabled 1") {
		t.Fatal(x.Metrics())
	}
	finish := func() {
		x.Start("https://rpc.example", "ethereum", []byte(requestBody)).Finish(200, []byte(successBody), "")
	}
	x.started = x.started.Add(-24 * time.Hour)
	x.total, x.failed = 39_999_999, 500_000
	finish()
	finish()
	x.total = 49_999_999
	finish()
	if len(lines) != 1 || !strings.Contains(lines[0], "hello@solidrpc.io") || !strings.Contains(lines[0], "DisableOffers") {
		t.Fatal(lines)
	}
}

func TestOfferRequiresProblemsVolumeAndConsent(t *testing.T) {
	lines := []string{}
	logger := func(line string) { lines = append(lines, line) }
	run := func(x *Xray, uptime time.Duration, total, failed uint64) {
		x.started = x.started.Add(-uptime)
		x.total, x.failed = total, failed
		x.Start("https://rpc.example", "ethereum", []byte(requestBody)).Finish(200, []byte(successBody), "")
	}
	run(New(Options{Logger: logger}), 24*time.Hour, 39_999_999, 0)
	run(New(Options{Logger: logger}), 30*24*time.Hour, 9_999, 5_000)
	off := New(Options{DisableOffers: true, Logger: logger})
	run(off, 24*time.Hour, 39_999_999, 5_000_000)
	if len(lines) != 0 || !strings.Contains(off.Metrics(), "rpc_xray_offers_enabled 0") {
		t.Fatal(lines)
	}
	panicky := New(Options{Logger: func(string) { panic("logger down") }})
	run(panicky, 24*time.Hour, 39_999_999, 5_000_000)
	if !panicky.offered {
		t.Fatal("offer not attempted")
	}
}

func TestExactNumericBatchIDs(t *testing.T) {
	for _, tc := range []struct{ headID, balanceID, returnedID string }{
		{"9223372036854775808", "9223372036854775809", "9223372036854775809"},
		{"-9223372036854775809", "-9223372036854775810", "-922337203685477581e1"},
		{"9223372036854775808e200", "9223372036854775809e200", "92233720368547758090e199"},
		{"0.10000000000000001", "0.10000000000000002", "10000000000000002e-17"},
		{"1e12345678912345678912", "1e12345678912345678913", "10e12345678912345678912"},
		{"1", "-0.0e2", "0"},
	} {
		req := parseRequest([]byte(`[{"id":` + tc.headID + `,"method":"eth_blockNumber"},{"id":` + tc.balanceID + `,"method":"eth_getBalance"}]`))
		response := []byte(`[{"id":` + tc.returnedID + `,"result":"0xeeee"}]`)
		want := []Entry{{"eth_blockNumber", "rpc_error"}, {"eth_getBalance", "ok"}}
		if got := classify(req, 200, response, ""); !reflect.DeepEqual(got, want) {
			t.Errorf("ids %+v got %v, want %v", tc, got, want)
		}
		if n, ok := headFrom(req, response); ok {
			t.Errorf("ids %+v assigned balance %d to head", tc, n)
		}
	}
}

func TestSingletonBatchClassification(t *testing.T) {
	req := parseRequest([]byte(`[{"id":1,"method":"eth_blockNumber"},{"id":2,"method":"eth_getBalance"}]`))
	for _, tc := range []struct{ response, outcome string }{
		{`{"id":1,"result":"0x10"}`, "rpc_error"},
		{`{"id":2,"result":"0xeeee"}`, "rpc_error"},
		{`{"id":2,"error":{"code":429}}`, "rpc_error"},
		{`{"id":null,"result":"0xeeee"}`, "rpc_error"},
		{`{"id":null,"error":{"code":429}}`, "rate_limited"},
		{`{"error":{"code":-32602}}`, "caller_error"},
	} {
		if got := classify(req, 200, []byte(tc.response), ""); !reflect.DeepEqual(got, []Entry{{"eth_blockNumber", tc.outcome}, {"eth_getBalance", tc.outcome}}) {
			t.Errorf("%s yielded %v", tc.response, got)
		}
	}
	if n, ok := headFrom(req, []byte(`{"id":1,"result":"0x10"}`)); !ok || n != 16 {
		t.Fatal(n, ok)
	}
}

func TestServedHeadRollbackAndRecovery(t *testing.T) {
	x := New(Options{})
	base := labels{chain: "ethereum", provider: "rpc.example"}
	now := time.Now()
	past := now.Add(-time.Minute)
	x.heads[base] = headState{block: 100, advanced: past, seen: past}
	x.heads[labels{chain: "ethereum", provider: "peer.example"}] = headState{block: 101, advanced: now, seen: now}
	for _, head := range []uint64{95, 95} {
		x.observeHead(base, head)
		state := x.heads[base]
		if state.advanced != past || !state.seen.After(past) || gaugeLine(x, "rpc_xray_head_block", "rpc.example") != "95" || gaugeLine(x, "rpc_xray_head_lag_blocks", "rpc.example") != "6" {
			t.Fatalf("rollback metrics: %s", x.Metrics())
		}
	}
	x.observeHead(base, 96)
	if !x.heads[base].advanced.After(past) || gaugeLine(x, "rpc_xray_head_lag_blocks", "rpc.example") != "5" {
		t.Fatalf("recovery metrics: %s", x.Metrics())
	}
}

func TestPassiveLatestBlockBatch(t *testing.T) {
	request := parseRequest([]byte(`[{"method":"eth_getBlockByNumber","params":["latest",false],"id":1},{"method":"eth_getBlockByNumber","params":["safe",false],"id":2},{"method":"eth_getBlockByNumber","params":["latest",false],"id":3}]`))
	head, ok := headFrom(request, []byte(`[{"id":2,"result":{"number":"0xffff"}},{"id":1,"result":{"number":"0x29"}},{"id":3,"result":{"number":"wrong"}},{"id":9,"result":{"number":"0xff"}}]`))
	if !ok || head != 41 {
		t.Fatalf("got head %d, present %t", head, ok)
	}
}
