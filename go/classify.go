package rpcxray

import (
	"encoding/json"
	"strconv"
	"strings"
)

var contract = func() struct {
	Methods        []string
	Chains         map[string]string
	Buckets        []float64
	CallerCodes    []float64
	RatePatterns   []string
	CallerPatterns []string
} {
	var value struct {
		Methods        []string
		Chains         map[string]string
		Buckets        []float64
		CallerCodes    []float64
		RatePatterns   []string
		CallerPatterns []string
	}
	if err := json.Unmarshal([]byte(contractJSON), &value); err != nil {
		panic(err)
	}
	return value
}()

func ChainLabel(id uint64) string {
	key := strconv.FormatUint(id, 10)
	if name, ok := contract.Chains[key]; ok {
		return name
	}
	return "eip155:" + key
}

type Entry struct {
	Method  string `json:"method"`
	Outcome string `json:"outcome"`
}
type call struct {
	id     string
	method string
}
type request struct {
	batch bool
	calls []call
}

func idKey(value any) string {
	if number, ok := value.(json.Number); ok {
		if integer, err := number.Int64(); err == nil {
			return strconv.FormatInt(integer, 10)
		}
		if numeric, err := number.Float64(); err == nil {
			return strconv.FormatFloat(numeric, 'f', -1, 64)
		}
	}
	encoded, _ := json.Marshal(value)
	return string(encoded)
}

func parseRequest(body []byte) *request {
	var payload any
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.UseNumber()
	if !json.Valid(body) || decoder.Decode(&payload) != nil {
		return nil
	}
	items, batch := payload.([]any)
	if !batch {
		items = []any{payload}
	}
	if len(items) == 0 {
		return nil
	}
	r := &request{batch: batch}
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return nil
		}
		method, ok := object["method"].(string)
		if !ok {
			return nil
		}
		known := false
		for _, name := range contract.Methods {
			if name == method {
				known = true
				break
			}
		}
		if !known {
			method = "other"
		}
		r.calls = append(r.calls, call{idKey(object["id"]), method})
	}
	return r
}

func rpcError(value any) string {
	object, _ := value.(map[string]any)
	code, hasCode := object["code"].(float64)
	message, _ := object["message"].(string)
	message = strings.ToLower(message)
	if code == 429 {
		return "rate_limited"
	}
	for _, pattern := range contract.RatePatterns {
		if strings.Contains(message, pattern) {
			return "rate_limited"
		}
	}
	if hasCode {
		for _, caller := range contract.CallerCodes {
			if code == caller {
				return "caller_error"
			}
		}
	}
	for _, pattern := range contract.CallerPatterns {
		if strings.Contains(message, pattern) {
			return "caller_error"
		}
	}
	return "rpc_error"
}

func responseOutcome(value any) string {
	object, ok := value.(map[string]any)
	if !ok {
		return "rpc_error"
	}
	if errorValue := object["error"]; errorValue != nil {
		return rpcError(errorValue)
	}
	if _, ok := object["result"]; ok {
		return "ok"
	}
	return "rpc_error"
}

func classify(r *request, status int, body []byte, failure string) []Entry {
	if r == nil {
		return []Entry{}
	}
	all := func(outcome string) []Entry {
		entries := make([]Entry, len(r.calls))
		for i, c := range r.calls {
			entries[i] = Entry{c.method, outcome}
		}
		return entries
	}
	if failure != "" {
		return all(failure)
	}
	if status == 429 {
		return all("rate_limited")
	}
	if status >= 400 {
		return all("http_error")
	}
	if body == nil {
		return all("ok")
	}
	var payload any
	if json.Unmarshal(body, &payload) != nil {
		return all("rpc_error")
	}
	items, batch := payload.([]any)
	if !batch {
		return all(responseOutcome(payload))
	}
	var rawItems []json.RawMessage
	if json.Unmarshal(body, &rawItems) != nil {
		return all("rpc_error")
	}
	byID := map[string]string{}
	for i, item := range items {
		if _, ok := item.(map[string]any); !ok {
			continue
		}
		var raw map[string]json.RawMessage
		_ = json.Unmarshal(rawItems[i], &raw)
		id := raw["id"]
		if id == nil {
			id = []byte("null")
		}
		var normalized any
		decoder := json.NewDecoder(strings.NewReader(string(id)))
		decoder.UseNumber()
		_ = decoder.Decode(&normalized)
		byID[idKey(normalized)] = responseOutcome(item)
	}
	entries := all("rpc_error")
	for i, c := range r.calls {
		if outcome, ok := byID[c.id]; ok {
			entries[i].Outcome = outcome
		}
	}
	return entries
}

func hexHead(value any) (uint64, bool) {
	text, ok := value.(string)
	if !ok || len(text) < 3 || len(text) > 18 || text[:2] != "0x" {
		return 0, false
	}
	head, err := strconv.ParseUint(text[2:], 16, 64)
	return head, err == nil
}

func headFrom(r *request, body []byte) (uint64, bool) {
	if r == nil || body == nil {
		return 0, false
	}
	ids := map[string]bool{}
	for _, c := range r.calls {
		if c.method == "eth_blockNumber" {
			ids[c.id] = true
		}
	}
	if len(ids) == 0 {
		return 0, false
	}
	var payload any
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.UseNumber()
	if decoder.Decode(&payload) != nil {
		return 0, false
	}
	items, batch := payload.([]any)
	if !batch {
		items = []any{payload}
	}
	var best uint64
	found := false
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok || object["error"] != nil {
			continue
		}
		if batch && !ids[idKey(object["id"])] {
			continue
		}
		if head, ok := hexHead(object["result"]); ok && (!found || head > best) {
			best, found = head, true
		}
	}
	return best, found
}
