package api

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client talks to the ARIA REST API.
type Client struct {
	Base   string
	APIKey string
	HTTP   *http.Client
}

func NewClient(base, apiKey string) *Client {
	return &Client{
		Base:   strings.TrimRight(base, "/"),
		APIKey: apiKey,
		HTTP:   &http.Client{Timeout: 30 * time.Second},
	}
}

// do executes a request, injecting the API key header.
func (c *Client) do(req *http.Request) (*http.Response, error) {
	if c.APIKey != "" {
		req.Header.Set("X-API-Key", c.APIKey)
	}
	return c.HTTP.Do(req)
}

// get is a convenience wrapper for GET requests with auth.
func (c *Client) get(url string) (*http.Response, error) {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	return c.do(req)
}

// post is a convenience wrapper for POST requests with auth.
func (c *Client) post(url, contentType string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequest("POST", url, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	return c.do(req)
}

// ---------- Conversations ----------

type ConversationStats struct {
	MessageCount int `json:"message_count"`
	TotalTokens  int `json:"total_tokens"`
	ToolCalls    int `json:"tool_calls"`
}

type Conversation struct {
	ID        string            `json:"id"`
	AgentID   string            `json:"agent_id"`
	Title     string            `json:"title"`
	Status    string            `json:"status"`
	Tags      []string          `json:"tags"`
	Pinned    bool              `json:"pinned"`
	CreatedAt time.Time         `json:"created_at"`
	UpdatedAt time.Time         `json:"updated_at"`
	Stats     ConversationStats `json:"stats"`
}

type LLMConfig struct {
	Backend     string  `json:"backend"`
	Model       string  `json:"model"`
	Temperature float64 `json:"temperature"`
}

type ConversationDetail struct {
	Conversation
	Messages  []Message `json:"messages"`
	LLMConfig LLMConfig `json:"llm_config"`
	Summary   string    `json:"summary,omitempty"`
}

type Message struct {
	ID        string    `json:"id"`
	Role      string    `json:"role"`
	Content   string    `json:"content"`
	Model     string    `json:"model,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

func (c *Client) ListConversations(limit, skip int, status string) ([]Conversation, error) {
	url := fmt.Sprintf("%s/api/v1/conversations?limit=%d&skip=%d&status=%s", c.Base, limit, skip, status)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var convs []Conversation
	return convs, json.NewDecoder(resp.Body).Decode(&convs)
}

func (c *Client) GetConversation(id string, msgLimit int) (*ConversationDetail, error) {
	url := fmt.Sprintf("%s/api/v1/conversations/%s?msg_limit=%d", c.Base, id, msgLimit)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)
	}
	var conv ConversationDetail
	return &conv, json.NewDecoder(resp.Body).Decode(&conv)
}

func (c *Client) CreateConversation(agentSlug, title string) (*ConversationDetail, error) {
	body := map[string]string{}
	if agentSlug != "" {
		body["agent_slug"] = agentSlug
	}
	if title != "" {
		body["title"] = title
	}
	b, _ := json.Marshal(body)
	resp, err := c.post(c.Base+"/api/v1/conversations", "application/json", bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var conv ConversationDetail
	return &conv, json.NewDecoder(resp.Body).Decode(&conv)
}

func (c *Client) DeleteConversation(id string) error {
	req, err := http.NewRequest("DELETE", fmt.Sprintf("%s/api/v1/conversations/%s", c.Base, id), nil)
	if err != nil {
		return err
	}
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != 204 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}

// ---------- Agents ----------

type Agent struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Slug         string `json:"slug"`
	Description  string `json:"description"`
	ModeCategory string `json:"mode_category"`
	Greeting     string `json:"greeting,omitempty"`
	IsDefault    bool   `json:"is_default"`
	LLM          struct {
		Backend string `json:"backend"`
		Model   string `json:"model"`
	} `json:"llm"`
}

func (c *Client) ListAgents() ([]Agent, error) {
	resp, err := c.get(c.Base + "/api/v1/agents")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var agents []Agent
	return agents, json.NewDecoder(resp.Body).Decode(&agents)
}

// ---------- Coding Sessions ----------

type CodingSession struct {
	ID          string     `json:"id"`
	Backend     string     `json:"backend"`
	Model       string     `json:"model,omitempty"`
	Workspace   string     `json:"workspace"`
	Prompt      string     `json:"prompt"`
	Branch      string     `json:"branch,omitempty"`
	PID         *int       `json:"pid,omitempty"`
	Status      string     `json:"status"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
	CompletedAt *time.Time `json:"completed_at,omitempty"`
}

func (c *Client) ListCodingSessions(status string) ([]CodingSession, error) {
	url := c.Base + "/api/v1/coding/sessions"
	if status != "" {
		url += "?status=" + status
	}
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var sessions []CodingSession
	return sessions, json.NewDecoder(resp.Body).Decode(&sessions)
}

func (c *Client) GetCodingOutput(sessionID string, lines int) (string, error) {
	url := fmt.Sprintf("%s/api/v1/coding/sessions/%s/output?lines=%d", c.Base, sessionID, lines)
	resp, err := c.get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var result struct {
		Output string `json:"output"`
		Lines  int    `json:"lines"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", err
	}
	return result.Output, nil
}

func (c *Client) SendCodingInput(sessionID, text string) error {
	body := map[string]string{"text": text}
	b, _ := json.Marshal(body)
	resp, err := c.post(
		fmt.Sprintf("%s/api/v1/coding/sessions/%s/input", c.Base, sessionID),
		"application/json", bytes.NewReader(b))
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

func (c *Client) StopCodingSession(sessionID string) error {
	resp, err := c.post(
		fmt.Sprintf("%s/api/v1/coding/sessions/%s/stop", c.Base, sessionID),
		"application/json", nil)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

// ---------- Memories ----------

type Memory struct {
	ID          string    `json:"id"`
	Content     string    `json:"content"`
	ContentType string    `json:"content_type"`
	Categories  []string  `json:"categories"`
	Confidence  float64   `json:"confidence"`
	Source      string    `json:"source,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
}

func (c *Client) ListMemories(limit, skip int) ([]Memory, error) {
	url := fmt.Sprintf("%s/api/v1/memories?limit=%d&skip=%d", c.Base, limit, skip)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var memories []Memory
	return memories, json.NewDecoder(resp.Body).Decode(&memories)
}

func (c *Client) SearchMemories(query string, limit int) ([]Memory, error) {
	body := map[string]interface{}{"query": query, "limit": limit}
	b, _ := json.Marshal(body)
	resp, err := c.post(c.Base+"/api/v1/memories/search", "application/json", bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var memories []Memory
	return memories, json.NewDecoder(resp.Body).Decode(&memories)
}

func (c *Client) DeleteMemory(id string) error {
	req, err := http.NewRequest("DELETE", fmt.Sprintf("%s/api/v1/memories/%s", c.Base, id), nil)
	if err != nil {
		return err
	}
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

// ---------- Usage ----------

type UsageSummary struct {
	TotalInputTokens  int `json:"total_input_tokens"`
	TotalOutputTokens int `json:"total_output_tokens"`
	TotalRequests     int `json:"total_requests"`
}

type AgentUsage struct {
	AgentID      string `json:"agent_id"`
	AgentName    string `json:"agent_name"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	Requests     int    `json:"requests"`
}

type ModelUsage struct {
	Backend      string `json:"backend"`
	Model        string `json:"model"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	Requests     int    `json:"requests"`
}

func (c *Client) GetUsage(hours int) (*UsageSummary, error) {
	url := fmt.Sprintf("%s/api/v1/usage/summary?hours=%d", c.Base, hours)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var u UsageSummary
	return &u, json.NewDecoder(resp.Body).Decode(&u)
}

func (c *Client) GetUsageByAgent(hours int) ([]AgentUsage, error) {
	url := fmt.Sprintf("%s/api/v1/usage/by-agent?hours=%d", c.Base, hours)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var usage []AgentUsage
	return usage, json.NewDecoder(resp.Body).Decode(&usage)
}

func (c *Client) GetUsageByModel(hours int) ([]ModelUsage, error) {
	url := fmt.Sprintf("%s/api/v1/usage/by-model?hours=%d", c.Base, hours)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var usage []ModelUsage
	return usage, json.NewDecoder(resp.Body).Decode(&usage)
}

// ---------- Streaming Messages (SSE) ----------

type StreamChunk struct {
	Type    string `json:"type"`
	Content string `json:"content,omitempty"`
	Error   string `json:"error,omitempty"`
}

// SendMessageStream sends a message and returns a channel of SSE events.
func (c *Client) SendMessageStream(conversationID, content string) (<-chan StreamChunk, error) {
	body := map[string]interface{}{
		"content": content,
		"stream":  true,
	}
	b, _ := json.Marshal(body)

	req, err := http.NewRequest("POST",
		fmt.Sprintf("%s/api/v1/conversations/%s/messages", c.Base, conversationID),
		bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if c.APIKey != "" {
		req.Header.Set("X-API-Key", c.APIKey)
	}

	streamClient := &http.Client{}
	resp, err := streamClient.Do(req)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != 200 {
		defer resp.Body.Close()
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, respBody)
	}

	ch := make(chan StreamChunk, 64)
	go func() {
		defer close(ch)
		defer resp.Body.Close()

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 0, 64*1024), 512*1024)

		for scanner.Scan() {
			line := scanner.Text()
			if strings.HasPrefix(line, "data:") {
				data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
				if data == "" {
					continue
				}
				var chunk StreamChunk
				if err := json.Unmarshal([]byte(data), &chunk); err != nil {
					continue
				}
				ch <- chunk
			}
		}
	}()

	return ch, nil
}

// ---------- Health ----------

type HealthStatus struct {
	Status   string `json:"status"`
	Version  string `json:"version"`
	Database string `json:"database"`
}

func (c *Client) Health() (*HealthStatus, error) {
	resp, err := c.get(c.Base + "/api/v1/health")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var h HealthStatus
	return &h, json.NewDecoder(resp.Body).Decode(&h)
}

type LLMBackendStatus struct {
	Backend   string `json:"backend"`
	Available bool   `json:"available"`
	Model     string `json:"model,omitempty"`
	Error     string `json:"error,omitempty"`
}

func (c *Client) HealthLLM() ([]LLMBackendStatus, error) {
	resp, err := c.get(c.Base + "/api/v1/health/llm")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var backends []LLMBackendStatus
	return backends, json.NewDecoder(resp.Body).Decode(&backends)
}

// ---------- Tools ----------

type Tool struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Type        string `json:"type"` // "builtin" or "mcp"
	Server      string `json:"server,omitempty"`
}

func (c *Client) ListTools() ([]Tool, error) {
	resp, err := c.get(c.Base + "/api/v1/tools")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var tools []Tool
	return tools, json.NewDecoder(resp.Body).Decode(&tools)
}

type MCPServer struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"`
	Tools  int    `json:"tool_count"`
}

func (c *Client) ListMCPServers() ([]MCPServer, error) {
	resp, err := c.get(c.Base + "/api/v1/mcp/servers")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var servers []MCPServer
	return servers, json.NewDecoder(resp.Body).Decode(&servers)
}

// ---------- Awareness ----------

type Observation struct {
	Sensor    string    `json:"sensor"`
	Category  string    `json:"category"`
	Summary   string    `json:"summary"`
	Severity  string    `json:"severity"`
	CreatedAt time.Time `json:"created_at"`
}

func (c *Client) ListObservations(limit int) ([]Observation, error) {
	url := fmt.Sprintf("%s/api/v1/awareness/observations?limit=%d", c.Base, limit)
	resp, err := c.get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var obs []Observation
	return obs, json.NewDecoder(resp.Body).Decode(&obs)
}

// ---------- Dashboard Snapshot ----------
// Batch fetch for efficiency (like ABP's DashboardSnapshot pattern)

type DashboardSnapshot struct {
	Health         *HealthStatus
	Agents         []Agent
	Conversations  []Conversation
	CodingSessions []CodingSession
	Usage          *UsageSummary
	Observations   []Observation
}

func (c *Client) FetchDashboardSnapshot() *DashboardSnapshot {
	snap := &DashboardSnapshot{}

	// Fire all requests (could parallelize with goroutines, but sequential is fine for ~6 calls)
	snap.Health, _ = c.Health()
	snap.Agents, _ = c.ListAgents()
	snap.Conversations, _ = c.ListConversations(50, 0, "active")
	snap.CodingSessions, _ = c.ListCodingSessions("")
	snap.Usage, _ = c.GetUsage(24)
	snap.Observations, _ = c.ListObservations(5)

	return snap
}
