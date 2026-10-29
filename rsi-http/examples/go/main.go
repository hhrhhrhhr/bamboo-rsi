// Standard-library-only control client. No Python runtime is needed in the caller.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"time"
)

func run(args []string) error {
	if len(args) != 3 && len(args) != 5 {
		return fmt.Errorf("usage: client <base-url> GET <path> | <base-url> POST <path> <json-file> <stable-idempotency-key>")
	}
	base, err := url.Parse(args[0])
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" || base.User != nil || base.RawQuery != "" || base.Fragment != "" || (base.Path != "" && base.Path != "/") {
		return fmt.Errorf("expected a credential-free HTTP(S) origin")
	}
	if !regexp.MustCompile(`^/v1/[a-z0-9/_.-]+$`).MatchString(args[2]) {
		return fmt.Errorf("expected an RSI control API path")
	}
	token := os.Getenv("RSI_CONTROL_TOKEN")
	if len(token) < 32 {
		return fmt.Errorf("set RSI_CONTROL_TOKEN in the process environment")
	}
	var body []byte
	if args[1] == "POST" && len(args) == 5 {
		if !regexp.MustCompile(`^[A-Za-z0-9_.:-]{16,128}$`).MatchString(args[4]) {
			return fmt.Errorf("keep the same idempotency key when recovering a lost response")
		}
		body, err = os.ReadFile(args[3])
		if err != nil || !json.Valid(body) {
			return fmt.Errorf("cannot read a valid JSON body file")
		}
	} else if args[1] != "GET" || len(args) != 3 {
		return fmt.Errorf("only GET or POST is supported")
	}
	endpoint := base.ResolveReference(&url.URL{Path: args[2]})
	request, err := http.NewRequest(args[1], endpoint.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Accept", "application/json")
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Idempotency-Key", args[4])
	}
	client := &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("control request failed; recover using the original operation or idempotency key")
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 4*1024*1024+1))
	if err != nil || len(data) > 4*1024*1024 {
		return fmt.Errorf("invalid or oversized response")
	}
	fmt.Println(string(data))
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusAccepted {
		return fmt.Errorf("control request returned status %d", response.StatusCode)
	}
	// 202 means queued, not successful. Poll GET /v1/operations/{id}.
	return nil
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
