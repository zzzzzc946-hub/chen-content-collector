import Foundation

enum RuntimeBootstrapError: LocalizedError {
    case bundleResourcesUnavailable
    case unsupportedArchitecture(String)
    case missingResource(name: String, path: String)
    case nonExecutableResource(name: String, path: String)
    case dataDirectoryUnavailable(path: String, detail: String)

    var errorDescription: String? {
        switch self {
        case .bundleResourcesUnavailable:
            return "无法读取 App 内嵌资源，请将 App 重新安装到“应用程序”文件夹后再试。"
        case .unsupportedArchitecture(let architecture):
            return "这台 Mac 的处理器架构（\(architecture)）不受支持；安装包仅支持 Apple 芯片和 Intel Mac。"
        case .missingResource(let name, let path):
            return "App 缺少\(name)：\(path)。请重新安装完整的 CHEN 内容采集助手。"
        case .nonExecutableResource(let name, let path):
            return "App 内嵌的\(name)不可执行：\(path)。请重新安装完整的 CHEN 内容采集助手。"
        case .dataDirectoryUnavailable(let path, let detail):
            return "无法准备用户数据目录 \(path)：\(detail)。请检查目录权限后重试。"
        }
    }
}

enum RuntimeArchitecture {
    static func current() -> String {
#if arch(arm64)
        return "arm64"
#elseif arch(x86_64)
        return "x86_64"
#else
        return "unsupported"
#endif
    }
}

struct RuntimeLayout {
    static let dataRootEnvironmentVariable = "CHEN_COLLECTOR_DATA_ROOT"

    let pythonExecutable: URL
    let collectorScript: URL
    let toolsDirectory: URL
    let dataDirectory: URL

    static func resolve(
        bundle: Bundle = .main,
        fileManager: FileManager = .default,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        architecture: String = RuntimeArchitecture.current()
    ) throws -> RuntimeLayout {
        guard let resources = bundle.resourceURL else {
            throw RuntimeBootstrapError.bundleResourcesUnavailable
        }

        let runtimePath: String
        switch architecture {
        case "arm64":
            runtimePath = "Runtime/arm64"
        case "x86_64":
            runtimePath = "Runtime/x86_64"
        default:
            throw RuntimeBootstrapError.unsupportedArchitecture(architecture)
        }

        let runtimeDirectory = resources.appendingPathComponent(runtimePath, isDirectory: true)
        let pythonExecutable = runtimeDirectory.appendingPathComponent("python/bin/python3")
        let collectorScript = resources.appendingPathComponent(
            "CollectorPayload/content_link_collector.py"
        )
        let toolsDirectory = runtimeDirectory.appendingPathComponent("tools", isDirectory: true)
        let ffmpegExecutable = toolsDirectory.appendingPathComponent("ffmpeg")

        try requireFile(pythonExecutable, named: "内嵌 Python", fileManager: fileManager)
        try requireExecutable(pythonExecutable, named: "内嵌 Python", fileManager: fileManager)
        try requireFile(collectorScript, named: "采集器脚本", fileManager: fileManager)
        try requireFile(ffmpegExecutable, named: "内嵌 ffmpeg", fileManager: fileManager)
        try requireExecutable(ffmpegExecutable, named: "内嵌 ffmpeg", fileManager: fileManager)

        let dataDirectory = homeDirectory
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("ChenContentLinkCollector", isDirectory: true)
        let logsDirectory = dataDirectory.appendingPathComponent("logs", isDirectory: true)
        try createUserOnlyDirectory(dataDirectory, fileManager: fileManager)
        try createUserOnlyDirectory(logsDirectory, fileManager: fileManager)

        return RuntimeLayout(
            pythonExecutable: pythonExecutable,
            collectorScript: collectorScript,
            toolsDirectory: toolsDirectory,
            dataDirectory: dataDirectory
        )
    }

    private static func requireFile(
        _ url: URL,
        named name: String,
        fileManager: FileManager
    ) throws {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory),
              !isDirectory.boolValue else {
            throw RuntimeBootstrapError.missingResource(name: name, path: url.path)
        }
    }

    private static func requireExecutable(
        _ url: URL,
        named name: String,
        fileManager: FileManager
    ) throws {
        guard fileManager.isExecutableFile(atPath: url.path) else {
            throw RuntimeBootstrapError.nonExecutableResource(name: name, path: url.path)
        }
    }

    private static func createUserOnlyDirectory(
        _ url: URL,
        fileManager: FileManager
    ) throws {
        let permissions = NSNumber(value: 0o700)
        do {
            try fileManager.createDirectory(
                at: url,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: permissions]
            )
            try fileManager.setAttributes(
                [.posixPermissions: permissions],
                ofItemAtPath: url.path
            )
        } catch {
            throw RuntimeBootstrapError.dataDirectoryUnavailable(
                path: url.path,
                detail: error.localizedDescription
            )
        }
    }
}
