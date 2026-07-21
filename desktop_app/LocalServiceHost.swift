import Darwin
import Foundation

struct ServiceConfiguration: Codable {
    let executable: String
    let arguments: [String]
    let workingDirectory: String
    let logPath: String
    let environment: [String: String]

    static func live(layout: RuntimeLayout) -> ServiceConfiguration {
        return ServiceConfiguration(
            executable: layout.pythonExecutable.path,
            arguments: [
                layout.collectorScript.path,
                "desktop-app",
                "--host",
                "127.0.0.1",
                "--port",
                "51216",
            ],
            workingDirectory: layout.dataDirectory.path,
            logPath: layout.dataDirectory
                .appendingPathComponent("logs/desktop-app.log")
                .path,
            environment: [
                RuntimeLayout.dataRootEnvironmentVariable: layout.dataDirectory.path,
                "PATH": "\(layout.toolsDirectory.path):/usr/bin:/bin:/usr/sbin:/sbin",
            ]
        )
    }
}

final class LocalServiceHost {
    enum HostError: LocalizedError {
        case missingScript(String)
        case portOccupied(pid: Int, command: String)
        case startupFailed(String)
        case healthTimeout(String)

        var errorDescription: String? {
            switch self {
            case .missingScript(let path):
                return "正式安装脚本不存在：\(path)"
            case .portOccupied(let pid, let command):
                return "本地端口 51216 已被其他程序占用（PID \(pid)：\(command)）。"
            case .startupFailed(let detail):
                return "本地服务启动失败：\(detail)"
            case .healthTimeout(let logPath):
                return "本地服务启动超时，请检查日志：\(logPath)。"
            }
        }
    }

    private let config: ServiceConfiguration
    private var process: Process?
    private var logHandle: FileHandle?

    init(configuration: ServiceConfiguration) {
        config = configuration
    }

    func configuration() -> ServiceConfiguration {
        config
    }

    func unloadLegacyLaunchAgent() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = [
            "bootout",
            "gui/\(getuid())/com.chen.content-link-collector.desktop-app",
        ]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
        task.waitUntilExit()
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void) {
        let scriptPath = config.arguments.first ?? ""
        guard FileManager.default.fileExists(atPath: scriptPath) else {
            completion(.failure(HostError.missingScript(scriptPath)))
            return
        }

        unloadLegacyLaunchAgent()
        if let owner = portOwner() {
            guard isKnownCollectorCommand(owner.command) else {
                completion(.failure(HostError.portOccupied(pid: owner.pid, command: owner.command)))
                return
            }
            terminateKnownCollector(pid: owner.pid)
        }

        do {
            let logURL = URL(fileURLWithPath: config.logPath)
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            let handle = try FileHandle(forWritingTo: logURL)
            try handle.seekToEnd()
            logHandle = handle

            let task = Process()
            task.executableURL = URL(fileURLWithPath: config.executable)
            task.arguments = config.arguments
            task.currentDirectoryURL = URL(fileURLWithPath: config.workingDirectory, isDirectory: true)
            task.environment = Self.mergedEnvironment(
                inherited: ProcessInfo.processInfo.environment,
                overriding: config.environment
            )
            task.standardOutput = handle
            task.standardError = handle
            process = task
            try task.run()
            waitForHealth(attempt: 0, completion: completion)
        } catch {
            stop()
            completion(.failure(HostError.startupFailed(error.localizedDescription)))
        }
    }

    func stop() {
        if let task = process, task.isRunning {
            task.terminate()
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
    }

    private func waitForHealth(attempt: Int, completion: @escaping (Result<Void, Error>) -> Void) {
        guard attempt < 60 else {
            completion(.failure(HostError.healthTimeout(config.logPath)))
            return
        }
        guard process?.isRunning == true else {
            completion(.failure(HostError.startupFailed("Python 进程已退出")))
            return
        }

        let request = URLRequest(
            url: URL(string: "http://127.0.0.1:51216/api/version")!,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 0.8
        )
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                DispatchQueue.main.async { completion(.success(())) }
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                self?.waitForHealth(attempt: attempt + 1, completion: completion)
            }
        }.resume()
    }

    private func portOwner() -> (pid: Int, command: String)? {
        let task = Process()
        let output = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-nP", "-tiTCP:51216", "-sTCP:LISTEN"]
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            guard let text = String(data: data, encoding: .utf8),
                  let first = text.split(separator: "\n").first,
                  let pid = Int(first) else {
                return nil
            }
            return (pid, command(for: pid))
        } catch {
            return nil
        }
    }

    private func command(for pid: Int) -> String {
        let task = Process()
        let output = Pipe()
        task.executableURL = URL(fileURLWithPath: "/bin/ps")
        task.arguments = ["-p", String(pid), "-o", "command="]
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "未知程序"
        } catch {
            return "未知程序"
        }
    }

    static func mergedEnvironment(
        inherited: [String: String],
        overriding configured: [String: String]
    ) -> [String: String] {
        inherited.merging(configured) { _, configuredValue in configuredValue }
    }

    func isKnownCollectorCommand(_ command: String) -> Bool {
        guard let currentScript = config.arguments.first,
              config.arguments.dropFirst().first == "desktop-app" else {
            return false
        }
        return Self.matchesCollectorInvocation(
            command,
            executable: config.executable,
            script: currentScript
        ) || Self.matchesCollectorInvocation(
            command,
            executable: "/usr/bin/python3",
            script: Self.legacyCollectorScript
        )
    }

    private static let legacyCollectorScript = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ChenContentLinkCollector/content_link_collector.py")
        .path

    private static func matchesCollectorInvocation(
        _ command: String,
        executable: String,
        script: String
    ) -> Bool {
        let command = command.trimmingCharacters(in: .whitespacesAndNewlines)
        let scriptRepresentations = [script, "\"\(script)\"", "'\(script)'"]

        for scriptRepresentation in scriptRepresentations {
            let invocation = "\(executable) \(scriptRepresentation) desktop-app"
            guard command.hasPrefix(invocation) else {
                continue
            }
            let nextIndex = command.index(command.startIndex, offsetBy: invocation.count)
            if nextIndex == command.endIndex || command[nextIndex].isWhitespace {
                return true
            }
        }
        return false
    }

    private func terminateKnownCollector(pid: Int) {
        kill(pid_t(pid), SIGTERM)
        for _ in 0..<20 where kill(pid_t(pid), 0) == 0 {
            usleep(50_000)
        }
    }
}
