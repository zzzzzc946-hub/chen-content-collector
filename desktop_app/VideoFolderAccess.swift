import AppKit
import Foundation

final class VideoFolderAccess {
    enum AccessError: LocalizedError {
        case cancelled
        case staleBookmark
        case unavailable

        var errorDescription: String? {
            switch self {
            case .cancelled:
                return "未授权视频目录，浏览日报不受影响，但本地视频暂时不能发布。"
            case .staleBookmark:
                return "原视频目录授权已失效，请重新选择视频文件夹。"
            case .unavailable:
                return "无法访问所选视频目录，请重新选择。"
            }
        }
    }

    private let bookmarkKey = "authorizedVideoFolderBookmark"
    private var activeURL: URL?

    func restoreOrRequest(completion: @escaping (Result<URL, Error>) -> Void) {
        dispatchPrecondition(condition: .onQueue(.main))
        if let restored = restoreBookmark(), activate(restored) {
            completion(.success(restored))
            return
        }
        requestFolder(completion: completion)
    }

    func requestFolder(completion: @escaping (Result<URL, Error>) -> Void) {
        dispatchPrecondition(condition: .onQueue(.main))
        let panel = NSOpenPanel()
        panel.title = "选择日报视频文件夹"
        panel.message = "请选择“文稿”中的视频目录。内容采集助手只读取发布所需视频，不会修改源文件。"
        panel.prompt = "授权此文件夹"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Documents", isDirectory: true)

        guard panel.runModal() == .OK, let folder = panel.url else {
            completion(.failure(AccessError.cancelled))
            return
        }

        do {
            let data = try folder.bookmarkData(
                options: [.withSecurityScope],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            UserDefaults.standard.set(data, forKey: bookmarkKey)
            guard activate(folder) else {
                completion(.failure(AccessError.unavailable))
                return
            }
            completion(.success(folder))
        } catch {
            completion(.failure(error))
        }
    }

    func stopAccessing() {
        activeURL?.stopAccessingSecurityScopedResource()
        activeURL = nil
    }

    private func restoreBookmark() -> URL? {
        guard let data = UserDefaults.standard.data(forKey: bookmarkKey) else {
            return nil
        }
        do {
            var stale = false
            let url = try URL(
                resolvingBookmarkData: data,
                options: [.withSecurityScope],
                relativeTo: nil,
                bookmarkDataIsStale: &stale
            )
            if stale {
                UserDefaults.standard.removeObject(forKey: bookmarkKey)
                return nil
            }
            return url
        } catch {
            UserDefaults.standard.removeObject(forKey: bookmarkKey)
            return nil
        }
    }

    private func activate(_ url: URL) -> Bool {
        stopAccessing()
        guard url.startAccessingSecurityScopedResource() else {
            return false
        }
        activeURL = url
        return true
    }
}
