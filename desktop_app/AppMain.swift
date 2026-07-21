import AppKit
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private let folderAccess = VideoFolderAccess()
    private var serviceHost: LocalServiceHost?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        showStatus("正在准备本地服务…")
        folderAccess.restoreOrRequest { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.presentAuthorizationNotice(error)
            }
            self.startService()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        serviceHost?.stop()
        folderAccess.stopAccessing()
    }

    @objc private func chooseVideoFolder() {
        folderAccess.requestFolder { [weak self] result in
            if case .failure(let error) = result {
                self?.presentAuthorizationNotice(error)
            }
        }
    }

    private func startService() {
        do {
            let layout = try RuntimeLayout.resolve()
            let host = LocalServiceHost(configuration: .live(layout: layout))
            serviceHost = host
            host.start { [weak self] result in
                switch result {
                case .success:
                    self?.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:51216/")!))
                case .failure(let error):
                    self?.showError(error.localizedDescription)
                }
            }
        } catch {
            showError(error.localizedDescription)
        }
    }

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.center()
        window.title = "CHEN 内容采集助手"
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func isLocalAppURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else {
            return false
        }
        return url.host == "127.0.0.1" && url.port == 51216
    }

    @discardableResult
    private func openExternalURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              !isLocalAppURL(url) else {
            return false
        }
        return NSWorkspace.shared.open(url)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if isLocalAppURL(url) {
            webView.load(navigationAction.request)
        } else {
            openExternalURL(url)
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if navigationAction.targetFrame?.isMainFrame == true,
           !isLocalAppURL(url),
           openExternalURL(url) {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    private func buildMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "CHEN 内容采集助手")
        let folderMenuItem = NSMenuItem(
            title: "重新选择视频文件夹…",
            action: #selector(chooseVideoFolder),
            keyEquivalent: ""
        )
        folderMenuItem.target = self
        appMenu.addItem(folderMenuItem)
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            NSMenuItem(
                title: "退出 CHEN 内容采集助手",
                action: #selector(NSApplication.terminate(_:)),
                keyEquivalent: "q"
            )
        )
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editMenuItem.submenu = editMenu
        NSApp.mainMenu = mainMenu
    }

    private func presentAuthorizationNotice(_ error: Error) {
        let alert = NSAlert()
        alert.messageText = "视频目录尚未授权"
        alert.informativeText = error.localizedDescription
        alert.alertStyle = .informational
        alert.addButton(withTitle: "继续浏览")
        alert.runModal()
    }

    private func showStatus(_ message: String) {
        let html = """
        <html><body style='font-family:-apple-system;padding:32px;background:#081120;color:#eef5ff'>
        <h2>CHEN 内容采集助手</h2><p>\(message)</p>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func showError(_ message: String) {
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let html = """
        <html><body style='font-family:-apple-system;padding:32px;background:#081120;color:#eef5ff'>
        <h2>CHEN 内容采集助手启动失败</h2><p>\(escaped)</p>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }
}

@main
struct ChenContentCollectorApplication {
    static func main() {
        if CommandLine.arguments.contains("--print-service-config") {
            do {
                let layout = try RuntimeLayout.resolve()
                let data = try JSONEncoder().encode(ServiceConfiguration.live(layout: layout))
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data("\n".utf8))
                return
            } catch {
                fputs("\(error.localizedDescription)\n", stderr)
                exit(1)
            }
        }

        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
