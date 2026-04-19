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
}
