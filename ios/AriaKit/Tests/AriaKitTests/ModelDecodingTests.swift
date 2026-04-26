import XCTest
@testable import AriaKit

final class ModelDecodingTests: XCTestCase {
    func testShellDecodesSnakeCaseDates() throws {
        let json = """
        {
          "name": "claude-proj",
          "short_name": "proj",
          "project_dir": "/tmp/proj",
          "host": "box",
          "status": "active",
          "created_at": "2026-04-18T12:00:00.123Z",
          "last_activity_at": "2026-04-18T12:05:00Z",
          "last_output_at": null,
          "last_input_at": null,
          "line_count": 42,
          "tags": ["primary"]
        }
        """.data(using: .utf8)!
        let shell = try AriaClient.makeDecoder().decode(Shell.self, from: json)
        XCTAssertEqual(shell.name, "claude-proj")
        XCTAssertEqual(shell.shortName, "proj")
        XCTAssertEqual(shell.status, .active)
        XCTAssertEqual(shell.lineCount, 42)
        XCTAssertEqual(shell.tags, ["primary"])
    }

    func testShellEventDecodes() throws {
        let json = """
        {
          "shell_name": "claude-proj",
          "ts": "2026-04-18T12:00:00Z",
          "line_number": 7,
          "kind": "output",
          "text_raw": "hello \\u001b[31mred\\u001b[0m",
          "text_clean": "hello red",
          "source": "pipe-pane",
          "byte_offset": 123
        }
        """.data(using: .utf8)!
        let evt = try AriaClient.makeDecoder().decode(ShellEvent.self, from: json)
        XCTAssertEqual(evt.lineNumber, 7)
        XCTAssertEqual(evt.kind, .output)
        XCTAssertEqual(evt.source, .pipePane)
        XCTAssertTrue(evt.textRaw.contains("\u{001B}[31m"))
    }

    func testShellCreateRequestEncodesSnakeCase() throws {
        let req = ShellCreateRequest(name: "newproj", workdir: "/tmp", launchClaude: false)
        let data = try AriaClient.makeEncoder().encode(req)
        let text = String(data: data, encoding: .utf8) ?? ""
        XCTAssertTrue(text.contains("\"launch_claude\":false"))
        XCTAssertTrue(text.contains("\"name\":\"newproj\""))
    }

    func testShellCreateRequestEncodesGeometry() throws {
        let req = ShellCreateRequest(name: "p", cols: 100, rows: 30)
        let data = try AriaClient.makeEncoder().encode(req)
        let text = String(data: data, encoding: .utf8) ?? ""
        XCTAssertTrue(text.contains("\"cols\":100"))
        XCTAssertTrue(text.contains("\"rows\":30"))
    }

    // The big one: Anthropic-shape tool calls (arguments as a JSON object).
    // Before the JSONValue change, this throw'd typeMismatch and broke the
    // whole Chat tab — see IMG_9879.
    func testToolCallDecodesObjectArguments() throws {
        let json = """
        {
          "id": "toolu_01",
          "name": "read_file",
          "arguments": {"path": "/etc/hosts", "lines": 5},
          "result": {"content": "127.0.0.1 localhost", "exit_code": 0}
        }
        """.data(using: .utf8)!
        let call = try AriaClient.makeDecoder().decode(ToolCall.self, from: json)
        XCTAssertEqual(call.name, "read_file")
        guard case .object(let args)? = call.arguments else {
            return XCTFail("expected object arguments, got \(String(describing: call.arguments))")
        }
        XCTAssertEqual(args["path"], .string("/etc/hosts"))
        guard case .object(let result)? = call.result else {
            return XCTFail("expected object result")
        }
        XCTAssertEqual(result["exit_code"], .int(0))
    }

    // OpenAI-shape: arguments as a JSON-encoded string.
    func testToolCallDecodesStringArguments() throws {
        let json = """
        {
          "id": "call_1",
          "name": "search",
          "arguments": "{\\"q\\":\\"hello\\"}",
          "result": "matched 3 docs"
        }
        """.data(using: .utf8)!
        let call = try AriaClient.makeDecoder().decode(ToolCall.self, from: json)
        XCTAssertEqual(call.arguments, .string("{\"q\":\"hello\"}"))
        XCTAssertEqual(call.result, .string("matched 3 docs"))
    }

    func testMessageDecodesWithToolCalls() throws {
        let json = """
        {
          "id": "m1",
          "role": "assistant",
          "content": "Looking that up.",
          "tool_calls": [
            {"id": "t1", "name": "web", "arguments": {}, "result": null}
          ]
        }
        """.data(using: .utf8)!
        let m = try AriaClient.makeDecoder().decode(Message.self, from: json)
        XCTAssertEqual(m.toolCalls?.count, 1)
        XCTAssertEqual(m.toolCalls?.first?.name, "web")
        XCTAssertEqual(m.toolCalls?.first?.result, JSONValue.null)
    }

    func testHealthStatusDecodesPlainStrings() throws {
        let json = """
        {
          "status": "degraded",
          "version": "0.2.0",
          "database": "connected",
          "embeddings": "timeout",
          "llm": "available (anthropic, openrouter)",
          "timestamp": "2026-04-18T12:00:00Z"
        }
        """.data(using: .utf8)!
        let h = try AriaClient.makeDecoder().decode(HealthStatus.self, from: json)
        XCTAssertEqual(h.status, "degraded")
        XCTAssertEqual(h.database, "connected")
        XCTAssertEqual(h.embeddings, "timeout")
        XCTAssertTrue(h.llm?.contains("anthropic") ?? false)
    }

    func testShellResizeRequestEncodes() throws {
        let req = ShellResizeRequest(cols: 132, rows: 50)
        let data = try AriaClient.makeEncoder().encode(req)
        let text = String(data: data, encoding: .utf8) ?? ""
        XCTAssertTrue(text.contains("\"cols\":132"))
        XCTAssertTrue(text.contains("\"rows\":50"))
    }

    func testJSONValueRoundTripsArray() throws {
        let json = "[1, \"two\", null, {\"k\": true}]".data(using: .utf8)!
        let v = try AriaClient.makeDecoder().decode(JSONValue.self, from: json)
        guard case .array(let items) = v else { return XCTFail("expected array") }
        XCTAssertEqual(items.count, 4)
        XCTAssertEqual(items[0], .int(1))
        XCTAssertEqual(items[1], .string("two"))
        XCTAssertEqual(items[2], .null)
        guard case .object(let obj) = items[3] else { return XCTFail("expected object") }
        XCTAssertEqual(obj["k"], .bool(true))
    }
}
