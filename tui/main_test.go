package main

import "testing"

// clearEnv neutralizes the ambient API env vars so resolveTarget is deterministic.
func clearEnv(t *testing.T) {
	t.Setenv("ARIA_API_URL", "")
	t.Setenv("ARIA_API_KEY", "")
	t.Setenv("API_KEY", "")
}

func TestResolveTarget(t *testing.T) {
	hosts := map[string]string{
		"default":     "corsair",
		"corsair.url": "http://corsair-ai:8200",
		"corsair.key": "profile-key",
		"local.url":   "http://localhost:8200",
	}

	tests := []struct {
		name     string
		hostFlag string
		keyFlag  string
		env      map[string]string
		dotenv   map[string]string
		wantURL  string
		wantKey  string
	}{
		{
			name:     "flag full URL",
			hostFlag: "http://box:9000",
			dotenv:   map[string]string{"API_KEY": "dot"},
			wantURL:  "http://box:9000",
			wantKey:  "dot",
		},
		{
			name:     "flag profile name",
			hostFlag: "corsair",
			wantURL:  "http://corsair-ai:8200",
			wantKey:  "profile-key",
		},
		{
			name:     "flag bare host:port",
			hostFlag: "corsair-ai:8200",
			wantURL:  "http://corsair-ai:8200",
			wantKey:  "",
		},
		{
			name:     "unknown profile falls back to default profile",
			hostFlag: "nope",
			wantURL:  "http://corsair-ai:8200",
			wantKey:  "profile-key",
		},
		{
			name:    "env URL beats default profile",
			env:     map[string]string{"ARIA_API_URL": "http://envhost:1", "ARIA_API_KEY": "envkey"},
			wantURL: "http://envhost:1",
			wantKey: "envkey",
		},
		{
			name:    "default profile when no flag/env",
			wantURL: "http://corsair-ai:8200",
			wantKey: "profile-key",
		},
		{
			name:     "keyFlag overrides profile key",
			hostFlag: "corsair",
			keyFlag:  "cli-key",
			wantURL:  "http://corsair-ai:8200",
			wantKey:  "cli-key",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			url, key := resolveTarget(tc.hostFlag, tc.keyFlag, tc.dotenv, hosts)
			if url != tc.wantURL {
				t.Errorf("url = %q, want %q", url, tc.wantURL)
			}
			if key != tc.wantKey {
				t.Errorf("key = %q, want %q", key, tc.wantKey)
			}
		})
	}
}

func TestResolveTargetLocalhostFallback(t *testing.T) {
	clearEnv(t)
	url, _ := resolveTarget("", "", nil, nil) // no flag, no env, no hosts file
	if url != "http://localhost:8200" {
		t.Errorf("url = %q, want localhost fallback", url)
	}
}
