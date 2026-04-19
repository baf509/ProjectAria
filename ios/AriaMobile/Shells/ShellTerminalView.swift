import SwiftUI
import SwiftTerm
import UIKit

@MainActor
final class TerminalBridge {
    weak var view: TerminalView?

    func feed(bytes: [UInt8]) {
        guard let view else { return }
        view.feed(byteArray: bytes[...])
    }

    func feed(string: String) {
        view?.feed(text: string)
    }

    func reset() {
        guard let view else { return }
        view.getTerminal().softReset()
    }

    func updateFont(name: String, size: CGFloat) {
        guard let view else { return }
        if let font = UIFont(name: name, size: size) ?? UIFont(name: "Menlo", size: size) {
            view.font = font
        }
    }
}

struct ShellTerminalView: UIViewRepresentable {
    let bridge: TerminalBridge
    let fontName: String
    let fontSize: CGFloat
    let onInput: (Data) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onInput: onInput) }

    func makeUIView(context: Context) -> TerminalView {
        let view = TerminalView()
        view.terminalDelegate = context.coordinator
        view.backgroundColor = UIColor(Neon.termBg)
        view.nativeBackgroundColor = UIColor(Neon.termBg)
        view.nativeForegroundColor = UIColor(Neon.termFg)
        if let font = UIFont(name: fontName, size: fontSize) ?? UIFont(name: "Menlo", size: fontSize) {
            view.font = font
        }
        bridge.view = view
        return view
    }

    func updateUIView(_ uiView: TerminalView, context: Context) {
        context.coordinator.onInput = onInput
        if let font = UIFont(name: fontName, size: fontSize), uiView.font != font {
            uiView.font = font
        }
    }

    final class Coordinator: NSObject, TerminalViewDelegate {
        var onInput: (Data) -> Void

        init(onInput: @escaping (Data) -> Void) { self.onInput = onInput }

        func send(source: TerminalView, data: ArraySlice<UInt8>) {
            onInput(Data(data))
        }
        func scrolled(source: TerminalView, position: Double) {}
        func setTerminalTitle(source: TerminalView, title: String) {}
        func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {}
        func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
        func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
            if let url = URL(string: link) {
                UIApplication.shared.open(url)
            }
        }
        func bell(source: TerminalView) {}
        func clipboardCopy(source: TerminalView, content: Data) {
            if let text = String(data: content, encoding: .utf8) {
                UIPasteboard.general.string = text
            }
        }
        func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    }
}
