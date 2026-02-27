import Cocoa
import Foundation

// MARK: - Naming Schemes

enum NamingScheme: String, CaseIterable {
    case prefixDate    = "prefix_date"     // {prefix}_2026-01-31_001.mp4
    case timestampFull = "timestamp_full"  // {PREFIX}_20260131_140144.mp4
    case datePrefix    = "date_prefix"     // 2026-01-31_14-01_{prefix}.mp4
    case originalCamera = "original"       // SCKR1000.mp4

    var displayName: String {
        switch self {
        case .prefixDate:     return "prefix_date_seq"
        case .timestampFull:  return "PREFIX_timestamp"
        case .datePrefix:     return "date_prefix"
        case .originalCamera: return "Camera original"
        }
    }

    func example(prefix: String) -> String {
        let p = prefix.isEmpty ? "cam" : prefix
        let short = String(p.prefix(3)).uppercased()
        switch self {
        case .prefixDate:     return "\(p)_2026-01-31_001.mp4"
        case .timestampFull:  return "\(short)_20260131_140144.mp4"
        case .datePrefix:     return "2026-01-31_14-01_\(p).mp4"
        case .originalCamera: return "SCKR1000.mp4"
        }
    }

    func generateName(prefix: String, date: Date?, ext: String, counter: Int) -> String {
        let p = prefix.isEmpty ? "cam" : prefix
        let short = String(p.prefix(3)).uppercased()
        let d = date ?? Date()
        let cal = Calendar.current
        let y = cal.component(.year, from: d)
        let mo = cal.component(.month, from: d)
        let dy = cal.component(.day, from: d)
        let h = cal.component(.hour, from: d)
        let mi = cal.component(.minute, from: d)
        let s = cal.component(.second, from: d)

        switch self {
        case .prefixDate:
            return String(format: "%@_%04d-%02d-%02d_%03d.\(ext)", p, y, mo, dy, counter)
        case .timestampFull:
            return String(format: "%@_%04d%02d%02d_%02d%02d%02d.\(ext)", short, y, mo, dy, h, mi, s)
        case .datePrefix:
            return String(format: "%04d-%02d-%02d_%02d-%02d_%@.\(ext)", y, mo, dy, h, mi, p)
        case .originalCamera:
            return "" // handled separately
        }
    }
}

// MARK: - Data Model

struct CameraFile {
    let name: String            // Original filename on camera
    let directory: String       // "101SYCAM" or "100SYCAM"
    let isVideo: Bool
    var isDownloaded: Bool = false
    var sizeBytes: Int64 = 0    // Size on camera
    var selected: Bool = true
    var localName: String       // Name to save as locally (naming scheme applied)
    var remoteTimestamp: Date?   // Timestamp from camera filesystem

    init(name: String, directory: String, isVideo: Bool) {
        self.name = name
        self.directory = directory
        self.isVideo = isVideo
        self.localName = name
    }

    var isRenamed: Bool { localName != name }

    var remotePath: String {
        "/mnt/mmc/DCIM/\(directory)/\(name)"
    }
    var httpPath: String {
        "\(directory)/\(name)"
    }
    var sizeString: String {
        if sizeBytes <= 0 { return "" }
        let mb = Double(sizeBytes) / 1_048_576.0
        if mb >= 1000 { return String(format: "%.1f GB", mb / 1024.0) }
        return String(format: "%.1f MB", mb)
    }
}

// MARK: - Camera Manager

class CameraManager {
    static let shared = CameraManager()

    var cameraIP: String {
        didSet { UserDefaults.standard.set(cameraIP, forKey: "HumDropCameraIP") }
    }
    var telnetPort = 23
    var httpPort = 8080
    var videoDir: URL
    var namingScheme: NamingScheme {
        didSet { UserDefaults.standard.set(namingScheme.rawValue, forKey: "HumDropNamingScheme") }
    }
    var namingPrefix: String {
        didSet { UserDefaults.standard.set(namingPrefix, forKey: "HumDropNamingPrefix") }
    }

    init() {
        namingScheme = NamingScheme(rawValue: UserDefaults.standard.string(forKey: "HumDropNamingScheme")
            ?? UserDefaults.standard.string(forKey: "HBNamingScheme") ?? "") ?? .prefixDate
        namingPrefix = UserDefaults.standard.string(forKey: "HumDropNamingPrefix") ?? "hummingbird"
        cameraIP = UserDefaults.standard.string(forKey: "HumDropCameraIP")
            ?? UserDefaults.standard.string(forKey: "HBCameraIP") ?? "192.168.1."
        if let savedDir = UserDefaults.standard.string(forKey: "HumDropVideoDir")
            ?? UserDefaults.standard.string(forKey: "HBVideoDir") {
            videoDir = URL(fileURLWithPath: savedDir)
        } else {
            videoDir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Desktop")
                .appendingPathComponent("Hummingbird")
                .appendingPathComponent("videos")
        }
        try? FileManager.default.createDirectory(at: videoDir, withIntermediateDirectories: true)
    }

    func changeVideoDir(to url: URL) {
        videoDir = url
        UserDefaults.standard.set(url.path, forKey: "HumDropVideoDir")
        try? FileManager.default.createDirectory(at: videoDir, withIntermediateDirectories: true)
    }

    // -------------------------------------------------------
    // Mirrors the original bash script's approach exactly:
    //   (sleep 1; echo "command"; sleep N) | nc IP 23
    // Each command gets a FRESH TCP connection, just like nc.
    // No telnet protocol handling — nc doesn't do it either.
    // -------------------------------------------------------

    // Persistent connection kept alive to hold httpd running
    private var persistentInput: InputStream?
    private var persistentOutput: OutputStream?

    // Open a raw TCP socket (like nc does — no IAC, no negotiation)
    private func openTCP(timeout: TimeInterval = 5) -> (InputStream, OutputStream)? {
        var inS: InputStream?
        var outS: OutputStream?
        Stream.getStreamsToHost(withName: cameraIP, port: telnetPort, inputStream: &inS, outputStream: &outS)
        guard let input = inS, let output = outS else { return nil }
        input.open()
        output.open()
        let deadline = Date().addingTimeInterval(timeout)
        while output.streamStatus != .open && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        guard output.streamStatus == .open else {
            input.close(); output.close(); return nil
        }
        return (input, output)
    }

    // Read everything available from a stream into Data
    private func drainStream(_ input: InputStream) -> Data {
        var result = Data()
        while input.hasBytesAvailable {
            var buf = [UInt8](repeating: 0, count: 8192)
            let n = input.read(&buf, maxLength: buf.count)
            if n > 0 { result.append(buf, count: n) } else { break }
        }
        return result
    }

    // Strip telnet IAC bytes (0xFF + 2 bytes) from raw data
    private func stripIAC(_ data: Data) -> String {
        var clean = Data()
        var i = 0
        while i < data.count {
            if data[i] == 0xFF && i + 2 < data.count { i += 3 }
            else { clean.append(data[i]); i += 1 }
        }
        return String(data: clean, encoding: .utf8) ?? String(data: clean, encoding: .ascii) ?? ""
    }

    // Run one command on a FRESH connection (like the original script does)
    private func runFreshCommand(_ command: String, readDelay: TimeInterval = 2.0) -> String {
        guard let (input, output) = openTCP(timeout: 3) else {
            appendDebugLog("[CMD] Failed to open TCP for: \(command)\n")
            return ""
        }
        Thread.sleep(forTimeInterval: 1.0)
        _ = drainStream(input)

        let cmd = command + "\n"
        output.write([UInt8](cmd.utf8), maxLength: cmd.utf8.count)

        Thread.sleep(forTimeInterval: readDelay)

        let raw = drainStream(input)
        input.close()
        output.close()

        let result = stripIAC(raw)
        appendDebugLog("[CMD] '\(command)' -> \(raw.count) bytes, \(result.count) chars\n")
        return result
    }

    func isCameraReachable() -> Bool {
        appendDebugLog("[REACH] Trying TCP to \(cameraIP):\(telnetPort)...\n")
        guard let (i, o) = openTCP(timeout: 5) else {
            appendDebugLog("[REACH] FAILED - could not open TCP\n")
            return false
        }
        i.close(); o.close()
        appendDebugLog("[REACH] OK - camera is reachable\n")
        return true
    }

    func startHTTPD() {
        persistentInput?.close()
        persistentOutput?.close()

        guard let (input, output) = openTCP(timeout: 5) else {
            appendDebugLog("[HTTPD] Failed to open TCP\n")
            return
        }
        persistentInput = input
        persistentOutput = output

        Thread.sleep(forTimeInterval: 1.0)
        let banner = stripIAC(drainStream(input))
        appendDebugLog("[HTTPD] Banner: \(String(banner.prefix(200)))\n")

        let cmd = "killall busybox 2>/dev/null; busybox httpd -p \(httpPort) -h /mnt/mmc/DCIM\n"
        output.write([UInt8](cmd.utf8), maxLength: cmd.utf8.count)

        Thread.sleep(forTimeInterval: 3.0)

        let ok = isHTTPDRunning()
        appendDebugLog("[HTTPD] Running: \(ok)\n")

        let lsResult = runFreshCommand("ls /mnt/mmc/DCIM/", readDelay: 2.0)
        appendDebugLog("[HTTPD] ls DCIM: \(String(lsResult.prefix(500)))\n")
    }

    func listFilesViaTelnet() -> [CameraFile] {
        var files: [CameraFile] = []

        let videoOutput = runFreshCommand("ls -la /mnt/mmc/DCIM/101SYCAM/", readDelay: 3.0)
        appendDebugLog("[TELNET videos] (\(videoOutput.count) chars): \(String(videoOutput.prefix(500)))\n")
        files.append(contentsOf: parseFilesFromLsLa(videoOutput, directory: "101SYCAM", ext: "mp4", isVideo: true))

        let photoOutput = runFreshCommand("ls -la /mnt/mmc/DCIM/100SYCAM/", readDelay: 3.0)
        appendDebugLog("[TELNET photos] (\(photoOutput.count) chars): \(String(photoOutput.prefix(500)))\n")
        files.append(contentsOf: parseFilesFromLsLa(photoOutput, directory: "100SYCAM", ext: "jpg", isVideo: false))

        return files.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    private func parseFilesFromLsLa(_ output: String, directory: String, ext: String, isVideo: Bool) -> [CameraFile] {
        let clean = output.replacingOccurrences(of: "\u{1B}\\[[\\d;]*m", with: "", options: .regularExpression)
        let lines = clean.components(separatedBy: "\n")
        var files: [CameraFile] = []
        var seen = Set<String>()

        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        let currentYear = Calendar.current.component(.year, from: Date())

        let linePattern = "(\\d+)\\s+(\\w{3})\\s+(\\d{1,2})\\s+(\\d{2}:\\d{2}|\\d{4})\\s+([\\w][\\w\\-.]+\\.\(ext))\\s*$"
        guard let regex = try? NSRegularExpression(pattern: linePattern, options: [.caseInsensitive]) else { return files }

        for line in lines {
            let range = NSRange(line.startIndex..., in: line)
            guard let match = regex.firstMatch(in: line, range: range),
                  let sizeRange = Range(match.range(at: 1), in: line),
                  let monRange = Range(match.range(at: 2), in: line),
                  let dayRange = Range(match.range(at: 3), in: line),
                  let timeRange = Range(match.range(at: 4), in: line),
                  let nameRange = Range(match.range(at: 5), in: line) else { continue }

            let filename = String(line[nameRange])
            guard seen.insert(filename).inserted else { continue }

            let sizeStr = String(line[sizeRange])
            let mon = String(line[monRange])
            let day = String(line[dayRange])
            let timeOrYear = String(line[timeRange])

            var f = CameraFile(name: filename, directory: directory, isVideo: isVideo)
            f.sizeBytes = Int64(sizeStr) ?? 0

            if timeOrYear.contains(":") {
                df.dateFormat = "MMM d HH:mm yyyy"
                f.remoteTimestamp = df.date(from: "\(mon) \(day) \(timeOrYear) \(currentYear)")
                if let d = f.remoteTimestamp, d > Date() {
                    f.remoteTimestamp = df.date(from: "\(mon) \(day) \(timeOrYear) \(currentYear - 1)")
                }
            } else {
                df.dateFormat = "MMM d yyyy"
                f.remoteTimestamp = df.date(from: "\(mon) \(day) \(timeOrYear)")
            }

            files.append(f)
        }

        if files.isEmpty {
            let plainPattern = "SCKR\\d+\\.\(ext)"
            if let r = try? NSRegularExpression(pattern: plainPattern, options: []) {
                let full = NSRange(clean.startIndex..., in: clean)
                let matches = r.matches(in: clean, range: full)
                for match in matches {
                    let name = String(clean[Range(match.range, in: clean)!])
                    if seen.insert(name).inserted {
                        files.append(CameraFile(name: name, directory: directory, isVideo: isVideo))
                    }
                }
            }
        }

        return files
    }

    // -------------------------------------------------------
    // Smart file comparison using SIZE-based matching.
    // -------------------------------------------------------

    private func buildLocalSizeMap() -> [Int64: String] {
        var map: [Int64: String] = [:]
        guard let contents = try? FileManager.default.contentsOfDirectory(atPath: videoDir.path) else { return map }
        for filename in contents {
            let ext = (filename as NSString).pathExtension.lowercased()
            guard ext == "mp4" || ext == "jpg" else { continue }
            let path = videoDir.appendingPathComponent(filename).path
            if let attrs = try? FileManager.default.attributesOfItem(atPath: path),
               let size = attrs[.size] as? Int64, size > 0 {
                map[size] = filename
            }
        }
        return map
    }

    func findNextCounter(forDate date: Date, ext: String) -> Int {
        let cal = Calendar.current
        let y = cal.component(.year, from: date)
        let mo = cal.component(.month, from: date)
        let dy = cal.component(.day, from: date)
        let dateStr = String(format: "%04d-%02d-%02d", y, mo, dy)

        var maxCounter = 0
        guard let contents = try? FileManager.default.contentsOfDirectory(atPath: videoDir.path) else { return 1 }

        for filename in contents {
            guard filename.contains(dateStr) && filename.hasSuffix(".\(ext)") else { continue }
            if let regex = try? NSRegularExpression(pattern: "_(\\d{3})\\.\(ext)$"),
               let match = regex.firstMatch(in: filename, range: NSRange(filename.startIndex..., in: filename)),
               let range = Range(match.range(at: 1), in: filename),
               let num = Int(filename[range]) {
                maxCounter = max(maxCounter, num)
            }
        }

        return maxCounter + 1
    }

    func resolveFileNames(_ files: inout [CameraFile]) {
        let localSizes = buildLocalSizeMap()
        let fm = FileManager.default
        var dateCounters: [String: Int] = [:]

        for i in 0..<files.count {
            let size = files[i].sizeBytes

            if size > 0, let localMatch = localSizes[size] {
                files[i].isDownloaded = true
                files[i].selected = false
                files[i].localName = localMatch
                appendDebugLog("[RESOLVE] \(files[i].name) -> already have \(localMatch) (size=\(size))\n")
                continue
            }

            files[i].isDownloaded = false
            files[i].selected = true

            if namingScheme == .originalCamera {
                files[i].localName = files[i].name
                var candidate = files[i].localName
                var n = 1
                while fm.fileExists(atPath: videoDir.appendingPathComponent(candidate).path) {
                    let base = (files[i].name as NSString).deletingPathExtension
                    let ext = files[i].isVideo ? "mp4" : "jpg"
                    candidate = "\(base)_\(n).\(ext)"
                    n += 1
                }
                files[i].localName = candidate
            } else {
                let ext = files[i].isVideo ? "mp4" : "jpg"
                let date = files[i].remoteTimestamp ?? Date()

                let cal = Calendar.current
                let dateKey = String(format: "%04d-%02d-%02d",
                                     cal.component(.year, from: date),
                                     cal.component(.month, from: date),
                                     cal.component(.day, from: date))

                if dateCounters[dateKey] == nil {
                    dateCounters[dateKey] = findNextCounter(forDate: date, ext: ext)
                }
                let counter = dateCounters[dateKey]!
                dateCounters[dateKey] = counter + 1

                let candidate = namingScheme.generateName(prefix: namingPrefix, date: date, ext: ext, counter: counter)
                files[i].localName = candidate
            }

            appendDebugLog("[RESOLVE] \(files[i].name) -> \(files[i].localName) (new)\n")
        }
    }

    func listFilesViaHTTP() -> [CameraFile] {
        var files: [CameraFile] = []

        let root = fetchURL("http://\(cameraIP):\(httpPort)/")
        appendDebugLog("[HTTP root] status=\(root.status) body=\(String(root.body.prefix(500)))\n")

        let videoFiles = fetchHTTPDirectoryListing(subdir: "101SYCAM", ext: "mp4", isVideo: true)
        files.append(contentsOf: videoFiles)

        let photoFiles = fetchHTTPDirectoryListing(subdir: "100SYCAM", ext: "jpg", isVideo: false)
        files.append(contentsOf: photoFiles)

        if files.isEmpty && root.status == 200 {
            appendDebugLog("[HTTP] Standard paths empty, scanning root for subdirs...\n")
            if let regex = try? NSRegularExpression(pattern: "href=\"([^\"]+)/\"", options: []) {
                let matches = regex.matches(in: root.body, range: NSRange(root.body.startIndex..., in: root.body))
                for match in matches {
                    if let range = Range(match.range(at: 1), in: root.body) {
                        let subdir = String(root.body[range])
                        if subdir.contains("..") { continue }
                        files.append(contentsOf: fetchHTTPDirectoryListing(subdir: subdir, ext: "mp4", isVideo: true))
                        files.append(contentsOf: fetchHTTPDirectoryListing(subdir: subdir, ext: "jpg", isVideo: false))
                    }
                }
            }
        }

        return files.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    private func fetchURL(_ urlString: String) -> (status: Int, body: String) {
        guard let url = URL(string: urlString) else { return (0, "") }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        var result: (Int, String) = (0, "")
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            result = (status, body)
            sem.signal()
        }.resume()
        sem.wait()
        return result
    }

    private func fetchHTTPDirectoryListing(subdir: String, ext: String, isVideo: Bool) -> [CameraFile] {
        let urlStr = "http://\(cameraIP):\(httpPort)/\(subdir)/"
        guard let url = URL(string: urlStr) else { return [] }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        var files: [CameraFile] = []

        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { [self] data, response, error in
            defer { sem.signal() }

            if let error = error {
                self.appendDebugLog("[HTTP \(subdir)] ERROR: \(error.localizedDescription)\n")
            } else if let http = response as? HTTPURLResponse {
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                self.appendDebugLog("[HTTP \(subdir)] status=\(http.statusCode) body=\(String(body.prefix(500)))\n")
            }

            guard let data = data, let html = String(data: data, encoding: .utf8) else { return }

            let pattern = "SCKR\\d+\\.\(ext)"
            guard let regex = try? NSRegularExpression(pattern: pattern, options: []) else { return }
            let matches = regex.matches(in: html, range: NSRange(html.startIndex..., in: html))
            var seen = Set<String>()
            for match in matches {
                let name = String(html[Range(match.range, in: html)!])
                if seen.insert(name).inserted {
                    var f = CameraFile(name: name, directory: subdir, isVideo: isVideo)
                    f.isDownloaded = FileManager.default.fileExists(atPath: self.videoDir.appendingPathComponent(name).path)
                    f.selected = !f.isDownloaded
                    files.append(f)
                }
            }
            if files.isEmpty {
                let broad = "[\\w\\-]+\\.\(ext)"
                if let br = try? NSRegularExpression(pattern: broad, options: [.caseInsensitive]) {
                    let bm = br.matches(in: html, range: NSRange(html.startIndex..., in: html))
                    for match in bm {
                        let name = String(html[Range(match.range, in: html)!])
                        if seen.insert(name).inserted {
                            var f = CameraFile(name: name, directory: subdir, isVideo: isVideo)
                            f.isDownloaded = FileManager.default.fileExists(atPath: self.videoDir.appendingPathComponent(name).path)
                            f.selected = !f.isDownloaded
                            files.append(f)
                        }
                    }
                }
            }
        }.resume()
        sem.wait()
        return files
    }

    func listFiles() -> [CameraFile] {
        appendDebugLog("=== listFiles() starting ===\n")

        var files = listFilesViaTelnet()
        appendDebugLog("Telnet found \(files.count) files\n")

        if files.isEmpty {
            files = listFilesViaHTTP()
            appendDebugLog("HTTP found \(files.count) files\n")
        }

        if !files.isEmpty {
            resolveFileNames(&files)
        }

        return files
    }

    func appendDebugLog(_ message: String) {
        let logFile = videoDir.deletingLastPathComponent().appendingPathComponent("debug.log")
        let timestamp = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        let line = "[\(timestamp)] \(message)"
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: logFile.path) {
                if let handle = try? FileHandle(forWritingTo: logFile) {
                    handle.seekToEndOfFile()
                    handle.write(data)
                    handle.closeFile()
                }
            } else {
                try? data.write(to: logFile)
            }
        }
    }

    func downloadFile(_ file: CameraFile, progress: @escaping (Double) -> Void, completion: @escaping (Bool) -> Void) {
        let url = URL(string: "http://\(cameraIP):\(httpPort)/\(file.httpPath)")!
        let dest = videoDir.appendingPathComponent(file.localName)
        let delegate = DownloadDelegate(destination: dest, cameraTimestamp: file.remoteTimestamp, progress: progress, completion: completion)
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        session.downloadTask(with: url).resume()
    }

    func deleteFile(_ file: CameraFile) {
        let result = runFreshCommand("rm -f \(file.remotePath)", readDelay: 1.0)
        appendDebugLog("[DELETE] \(file.name): \(result)\n")
    }

    func wipeAll() {
        let r1 = runFreshCommand("rm -f /mnt/mmc/DCIM/101SYCAM/*", readDelay: 2.0)
        appendDebugLog("[WIPE] videos: \(r1)\n")
        let r2 = runFreshCommand("rm -f /mnt/mmc/DCIM/100SYCAM/*", readDelay: 2.0)
        appendDebugLog("[WIPE] photos: \(r2)\n")
    }

    func cleanup() {
        persistentInput?.close()
        persistentOutput?.close()
        persistentInput = nil
        persistentOutput = nil
    }
}

// MARK: - Download Delegate

class DownloadDelegate: NSObject, URLSessionDownloadDelegate {
    let destination: URL
    let cameraTimestamp: Date?
    let progressHandler: (Double) -> Void
    let completionHandler: (Bool) -> Void

    init(destination: URL, cameraTimestamp: Date?, progress: @escaping (Double) -> Void, completion: @escaping (Bool) -> Void) {
        self.destination = destination
        self.cameraTimestamp = cameraTimestamp
        self.progressHandler = progress
        self.completionHandler = completion
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(at: location, to: destination)
            let attrs = try FileManager.default.attributesOfItem(atPath: destination.path)
            let size = attrs[.size] as? Int64 ?? 0

            if let ts = cameraTimestamp {
                try? FileManager.default.setAttributes(
                    [.modificationDate: ts, .creationDate: ts],
                    ofItemAtPath: destination.path)
            }

            completionHandler(size > 0)
        } catch {
            completionHandler(false)
        }
        session.invalidateAndCancel()
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesExpectedToWrite > 0 {
            progressHandler(Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if error != nil {
            completionHandler(false)
        }
    }
}

// MARK: - App Icon Generator

func createAppIcon() -> NSImage {
    let size: CGFloat = 512
    let img = NSImage(size: NSSize(width: size, height: size))
    img.lockFocus()

    // Background: warm gradient rounded square
    let bgRect = NSRect(x: 0, y: 0, width: size, height: size)
    let radius = size * 0.22
    let bg = NSBezierPath(roundedRect: bgRect, xRadius: radius, yRadius: radius)

    let topColor = NSColor(calibratedRed: 0.18, green: 0.85, blue: 0.72, alpha: 1.0)    // bright teal
    let bottomColor = NSColor(calibratedRed: 0.08, green: 0.38, blue: 0.50, alpha: 1.0)  // deep teal
    NSGradient(starting: bottomColor, ending: topColor)!.draw(in: bg, angle: 90)

    // Subtle border
    NSColor.white.withAlphaComponent(0.2).setStroke()
    bg.lineWidth = 2
    bg.stroke()

    // Branch / perch line - a gentle arc across the middle
    let branchColor = NSColor(calibratedRed: 0.40, green: 0.22, blue: 0.10, alpha: 0.7)
    branchColor.setStroke()
    let branch = NSBezierPath()
    branch.move(to: NSPoint(x: size * 0.08, y: size * 0.38))
    branch.curve(to: NSPoint(x: size * 0.92, y: size * 0.42),
                 controlPoint1: NSPoint(x: size * 0.35, y: size * 0.32),
                 controlPoint2: NSPoint(x: size * 0.65, y: size * 0.48))
    branch.lineWidth = size * 0.025
    branch.lineCapStyle = .round
    branch.stroke()

    // Small twig off the branch
    let twig = NSBezierPath()
    twig.move(to: NSPoint(x: size * 0.72, y: size * 0.43))
    twig.line(to: NSPoint(x: size * 0.78, y: size * 0.52))
    twig.lineWidth = size * 0.012
    twig.lineCapStyle = .round
    twig.stroke()

    // Bird body - sitting on the branch
    let birdCX = size * 0.50
    let birdCY = size * 0.52

    // Body (oval, slightly tilted)
    NSColor.white.withAlphaComponent(0.95).setFill()
    let bodyW = size * 0.18
    let bodyH = size * 0.22
    let bodyPath = NSBezierPath(ovalIn: NSRect(x: birdCX - bodyW/2, y: birdCY - bodyH * 0.3, width: bodyW, height: bodyH))
    bodyPath.fill()

    // Head (smaller circle overlapping top of body)
    let headR = size * 0.075
    let headCX = birdCX + size * 0.02
    let headCY = birdCY + bodyH * 0.55
    NSColor.white.withAlphaComponent(0.97).setFill()
    NSBezierPath(ovalIn: NSRect(x: headCX - headR, y: headCY - headR, width: headR * 2, height: headR * 2)).fill()

    // Eye
    let eyeR = size * 0.014
    let eyeX = headCX + headR * 0.35
    let eyeY = headCY + headR * 0.15
    NSColor(calibratedRed: 0.2, green: 0.15, blue: 0.1, alpha: 0.9).setFill()
    NSBezierPath(ovalIn: NSRect(x: eyeX - eyeR, y: eyeY - eyeR, width: eyeR * 2, height: eyeR * 2)).fill()

    // Eye highlight
    let hlR = eyeR * 0.4
    NSColor.white.setFill()
    NSBezierPath(ovalIn: NSRect(x: eyeX - hlR + eyeR * 0.3, y: eyeY - hlR + eyeR * 0.3, width: hlR * 2, height: hlR * 2)).fill()

    // Beak
    let beakPath = NSBezierPath()
    NSColor(calibratedRed: 0.95, green: 0.65, blue: 0.15, alpha: 1.0).setFill()
    beakPath.move(to: NSPoint(x: headCX + headR * 0.8, y: headCY))
    beakPath.line(to: NSPoint(x: headCX + headR * 1.6, y: headCY + headR * 0.1))
    beakPath.line(to: NSPoint(x: headCX + headR * 0.8, y: headCY - headR * 0.25))
    beakPath.close()
    beakPath.fill()

    // Tail feathers (extending left from body)
    NSColor.white.withAlphaComponent(0.85).setFill()
    let tailPath = NSBezierPath()
    tailPath.move(to: NSPoint(x: birdCX - bodyW * 0.35, y: birdCY))
    tailPath.line(to: NSPoint(x: birdCX - bodyW * 1.1, y: birdCY + bodyH * 0.1))
    tailPath.line(to: NSPoint(x: birdCX - bodyW * 1.0, y: birdCY - bodyH * 0.05))
    tailPath.line(to: NSPoint(x: birdCX - bodyW * 0.35, y: birdCY - bodyH * 0.1))
    tailPath.close()
    tailPath.fill()

    // Bird legs on branch
    NSColor(calibratedRed: 0.40, green: 0.22, blue: 0.10, alpha: 0.8).setStroke()
    let legPath = NSBezierPath()
    legPath.move(to: NSPoint(x: birdCX - size * 0.025, y: birdCY - bodyH * 0.2))
    legPath.line(to: NSPoint(x: birdCX - size * 0.03, y: size * 0.40))
    legPath.move(to: NSPoint(x: birdCX + size * 0.025, y: birdCY - bodyH * 0.2))
    legPath.line(to: NSPoint(x: birdCX + size * 0.02, y: size * 0.41))
    legPath.lineWidth = size * 0.010
    legPath.lineCapStyle = .round
    legPath.stroke()

    // Sync arrows below the branch
    let arrowColor = NSColor.white.withAlphaComponent(0.85)
    arrowColor.setStroke()
    let arcR: CGFloat = size * 0.07
    let arcCX = size / 2
    let arcCY = size * 0.22
    let arcPath = NSBezierPath()
    arcPath.appendArc(withCenter: NSPoint(x: arcCX, y: arcCY), radius: arcR,
                      startAngle: 30, endAngle: 300, clockwise: false)
    arcPath.lineWidth = size * 0.016
    arcPath.lineCapStyle = .round
    arcPath.stroke()

    // Arrowhead
    arrowColor.setFill()
    let arrowTip = NSPoint(x: arcCX + arcR * cos(30 * .pi / 180),
                           y: arcCY + arcR * sin(30 * .pi / 180))
    let arrow = NSBezierPath()
    arrow.move(to: arrowTip)
    arrow.line(to: NSPoint(x: arrowTip.x - size * 0.025, y: arrowTip.y + size * 0.018))
    arrow.line(to: NSPoint(x: arrowTip.x + size * 0.008, y: arrowTip.y + size * 0.03))
    arrow.close()
    arrow.fill()

    // "PERCH" text at the top
    let textAttrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.boldSystemFont(ofSize: size * 0.065),
        .foregroundColor: NSColor.white.withAlphaComponent(0.9),
        .kern: size * 0.02
    ]
    let textStr = "HUMDROP" as NSString
    let textSize = textStr.size(withAttributes: textAttrs)
    textStr.draw(at: NSPoint(x: (size - textSize.width) / 2, y: size * 0.82), withAttributes: textAttrs)

    img.unlockFocus()
    return img
}

// MARK: - Accent Colors

struct HumDropColors {
    static let accent = NSColor(calibratedRed: 0.10, green: 0.72, blue: 0.62, alpha: 1.0)  // teal
    static let accentLight = NSColor(calibratedRed: 0.20, green: 0.82, blue: 0.70, alpha: 1.0)
    static let headerBg = NSColor(calibratedRed: 0.14, green: 0.14, blue: 0.16, alpha: 1.0) // dark charcoal
    static let headerText = NSColor.white
    static let connected = NSColor(calibratedRed: 0.30, green: 0.85, blue: 0.45, alpha: 1.0)
    static let disconnected = NSColor(calibratedRed: 0.95, green: 0.30, blue: 0.25, alpha: 1.0)
    static let newFile = NSColor(calibratedRed: 0.30, green: 0.85, blue: 0.45, alpha: 1.0)
}

// MARK: - Main Window Controller

class MainWindowController: NSWindowController {
    let camera = CameraManager.shared
    var files: [CameraFile] = []
    var isConnected = false
    var isDownloading = false

    // UI Elements
    var statusDot: NSView!
    var statusLabel: NSTextField!
    var ipLabel: NSTextField!
    var ipField: NSTextField!
    var connectButton: NSButton!
    var aboutButton: NSButton!
    var tableView: NSTableView!
    var scrollView: NSScrollView!
    var summaryLabel: NSTextField!
    var progressBar: NSProgressIndicator!
    var progressLabel: NSTextField!
    var downloadButton: NSButton!
    var cleanButton: NSButton!
    var wipeButton: NSButton!
    var refreshButton: NSButton!
    var openFolderButton: NSButton!
    var selectAllButton: NSButton!
    var findButton: NSButton!
    var folderLabel: NSTextField!
    var folderPathControl: NSPathControl!
    var namingPopup: NSPopUpButton!
    var prefixField: NSTextField!
    var prefixLabel: NSTextField!
    var exampleLabel: NSTextField!

    // Section views
    var headerView: NSView!
    var instructionView: NSView!
    var instructionLabel: NSTextField!
    var fileSectionViews: [NSView] = []

    convenience init() {
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 660, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        w.title = "HumDrop"
        w.center()
        w.minSize = NSSize(width: 540, height: 520)
        w.isReleasedWhenClosed = false

        self.init(window: w)
        setupUI()
        updateConnectionStatus(connected: false)
    }

    private func setupUI() {
        guard let content = window?.contentView else { return }
        content.wantsLayer = true

        // -- Teal accent strip at top --
        let accentStrip = NSView()
        accentStrip.translatesAutoresizingMaskIntoConstraints = false
        accentStrip.wantsLayer = true
        accentStrip.layer?.backgroundColor = HumDropColors.accent.cgColor
        content.addSubview(accentStrip)

        NSLayoutConstraint.activate([
            accentStrip.topAnchor.constraint(equalTo: content.topAnchor),
            accentStrip.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            accentStrip.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            accentStrip.heightAnchor.constraint(equalToConstant: 4),
        ])

        // -- Header area --
        headerView = NSView()
        headerView.translatesAutoresizingMaskIntoConstraints = false
        headerView.wantsLayer = true
        headerView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        content.addSubview(headerView)

        let titleLabel = NSTextField(labelWithString: "HumDrop")
        titleLabel.translatesAutoresizingMaskIntoConstraints = false
        titleLabel.font = NSFont.systemFont(ofSize: 22, weight: .bold)
        titleLabel.textColor = .labelColor
        titleLabel.isSelectable = false
        headerView.addSubview(titleLabel)

        let subtitleLabel = NSTextField(labelWithString: "Camera Sync")
        subtitleLabel.translatesAutoresizingMaskIntoConstraints = false
        subtitleLabel.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        subtitleLabel.textColor = HumDropColors.accent
        subtitleLabel.isSelectable = false
        headerView.addSubview(subtitleLabel)

        ipLabel = makeLabel("IP:", size: 11, color: .secondaryLabelColor)
        headerView.addSubview(ipLabel)

        ipField = NSTextField()
        ipField.translatesAutoresizingMaskIntoConstraints = false
        ipField.stringValue = camera.cameraIP
        ipField.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        ipField.isBordered = true
        ipField.isBezeled = true
        ipField.bezelStyle = .roundedBezel
        ipField.isEditable = true
        ipField.placeholderString = "192.168.1.XX"
        ipField.target = self
        ipField.action = #selector(ipChanged)
        headerView.addSubview(ipField)

        findButton = NSButton(title: "Find", target: self, action: #selector(findCameraTapped))
        findButton.translatesAutoresizingMaskIntoConstraints = false
        findButton.bezelStyle = .rounded
        findButton.controlSize = .small
        findButton.font = NSFont.systemFont(ofSize: 10)
        headerView.addSubview(findButton)

        folderLabel = makeLabel("Save to:", size: 12, color: .secondaryLabelColor)
        headerView.addSubview(folderLabel)

        folderPathControl = NSPathControl()
        folderPathControl.translatesAutoresizingMaskIntoConstraints = false
        folderPathControl.pathStyle = .standard
        folderPathControl.isEditable = false
        folderPathControl.url = camera.videoDir
        folderPathControl.target = self
        folderPathControl.action = #selector(changeFolderTapped)
        folderPathControl.font = NSFont.systemFont(ofSize: 12)
        folderPathControl.controlSize = .regular
        headerView.addSubview(folderPathControl)

        statusDot = NSView()
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 5
        statusDot.layer?.backgroundColor = HumDropColors.disconnected.cgColor
        headerView.addSubview(statusDot)

        statusLabel = makeLabel("Disconnected", size: 12, color: .secondaryLabelColor)
        statusLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        headerView.addSubview(statusLabel)

        connectButton = NSButton(title: "Connect", target: self, action: #selector(connectTapped))
        connectButton.translatesAutoresizingMaskIntoConstraints = false
        connectButton.bezelStyle = .rounded
        connectButton.controlSize = .regular
        headerView.addSubview(connectButton)

        aboutButton = NSButton(title: "About", target: self, action: #selector(showAbout))
        aboutButton.translatesAutoresizingMaskIntoConstraints = false
        aboutButton.bezelStyle = .rounded
        aboutButton.controlSize = .small
        aboutButton.font = NSFont.systemFont(ofSize: 11)
        headerView.addSubview(aboutButton)

        NSLayoutConstraint.activate([
            headerView.topAnchor.constraint(equalTo: accentStrip.bottomAnchor),
            headerView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            headerView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            headerView.heightAnchor.constraint(equalToConstant: 114),

            titleLabel.topAnchor.constraint(equalTo: headerView.topAnchor, constant: 12),
            titleLabel.leadingAnchor.constraint(equalTo: headerView.leadingAnchor, constant: 20),

            subtitleLabel.leadingAnchor.constraint(equalTo: titleLabel.trailingAnchor, constant: 6),
            subtitleLabel.bottomAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: -2),

            ipLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 6),
            ipLabel.leadingAnchor.constraint(equalTo: headerView.leadingAnchor, constant: 20),

            ipField.centerYAnchor.constraint(equalTo: ipLabel.centerYAnchor),
            ipField.leadingAnchor.constraint(equalTo: ipLabel.trailingAnchor, constant: 6),
            ipField.widthAnchor.constraint(equalToConstant: 130),

            findButton.centerYAnchor.constraint(equalTo: ipLabel.centerYAnchor),
            findButton.leadingAnchor.constraint(equalTo: ipField.trailingAnchor, constant: 4),

            statusDot.centerYAnchor.constraint(equalTo: ipLabel.centerYAnchor),
            statusDot.leadingAnchor.constraint(equalTo: findButton.trailingAnchor, constant: 10),
            statusDot.widthAnchor.constraint(equalToConstant: 10),
            statusDot.heightAnchor.constraint(equalToConstant: 10),

            statusLabel.centerYAnchor.constraint(equalTo: statusDot.centerYAnchor),
            statusLabel.leadingAnchor.constraint(equalTo: statusDot.trailingAnchor, constant: 6),

            folderLabel.topAnchor.constraint(equalTo: ipLabel.bottomAnchor, constant: 8),
            folderLabel.leadingAnchor.constraint(equalTo: headerView.leadingAnchor, constant: 20),

            folderPathControl.centerYAnchor.constraint(equalTo: folderLabel.centerYAnchor),
            folderPathControl.leadingAnchor.constraint(equalTo: folderLabel.trailingAnchor, constant: 6),
            folderPathControl.trailingAnchor.constraint(equalTo: headerView.trailingAnchor, constant: -20),
            folderPathControl.heightAnchor.constraint(equalToConstant: 24),

            connectButton.topAnchor.constraint(equalTo: headerView.topAnchor, constant: 14),
            connectButton.trailingAnchor.constraint(equalTo: headerView.trailingAnchor, constant: -20),

            aboutButton.topAnchor.constraint(equalTo: connectButton.bottomAnchor, constant: 2),
            aboutButton.trailingAnchor.constraint(equalTo: headerView.trailingAnchor, constant: -20),
        ])

        // -- Instruction overlay (shown when disconnected) --
        instructionView = NSView()
        instructionView.translatesAutoresizingMaskIntoConstraints = false
        instructionView.wantsLayer = true
        content.addSubview(instructionView)

        instructionLabel = NSTextField(wrappingLabelWithString: "")
        instructionLabel.translatesAutoresizingMaskIntoConstraints = false
        instructionLabel.font = NSFont.systemFont(ofSize: 13)
        instructionLabel.textColor = .secondaryLabelColor
        instructionLabel.alignment = .center
        instructionLabel.isSelectable = false
        instructionView.addSubview(instructionLabel)
        updateInstructionText()

        // Bird emoji
        let birdLabel = makeLabel("\u{1F426}", size: 48)
        birdLabel.alignment = .center
        instructionView.addSubview(birdLabel)

        NSLayoutConstraint.activate([
            instructionView.topAnchor.constraint(equalTo: headerView.bottomAnchor),
            instructionView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            instructionView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            instructionView.bottomAnchor.constraint(equalTo: content.bottomAnchor),

            birdLabel.centerXAnchor.constraint(equalTo: instructionView.centerXAnchor),
            birdLabel.centerYAnchor.constraint(equalTo: instructionView.centerYAnchor, constant: -60),

            instructionLabel.topAnchor.constraint(equalTo: birdLabel.bottomAnchor, constant: 16),
            instructionLabel.centerXAnchor.constraint(equalTo: instructionView.centerXAnchor),
            instructionLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 380),
        ])

        // -- File table section (hidden when disconnected) --
        let fileHeaderLabel = makeLabel("Files", size: 14, bold: true)
        content.addSubview(fileHeaderLabel)

        selectAllButton = makeButton("Select All", action: #selector(selectAllFiles))
        selectAllButton.controlSize = .small
        selectAllButton.font = NSFont.systemFont(ofSize: 11)
        content.addSubview(selectAllButton)

        let selectNewButton = makeButton("Select New Only", action: #selector(selectAllNew))
        selectNewButton.controlSize = .small
        selectNewButton.font = NSFont.systemFont(ofSize: 11)
        content.addSubview(selectNewButton)

        scrollView = NSScrollView()
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.drawsBackground = true
        content.addSubview(scrollView)

        tableView = NSTableView()
        if #available(macOS 11.0, *) {
            tableView.style = .fullWidth
        }
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.rowHeight = 26
        tableView.gridStyleMask = .solidHorizontalGridLineMask

        let checkCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("check"))
        checkCol.title = ""
        checkCol.width = 30
        checkCol.minWidth = 30
        checkCol.maxWidth = 30
        tableView.addTableColumn(checkCol)

        let cameraCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("camera"))
        cameraCol.title = "Camera File"
        cameraCol.width = 130
        cameraCol.minWidth = 80
        tableView.addTableColumn(cameraCol)

        let saveAsCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("saveas"))
        saveAsCol.title = "Save As"
        saveAsCol.width = 180
        saveAsCol.minWidth = 100
        tableView.addTableColumn(saveAsCol)

        let sizeCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("size"))
        sizeCol.title = "Size"
        sizeCol.width = 70
        sizeCol.minWidth = 50
        sizeCol.maxWidth = 90
        tableView.addTableColumn(sizeCol)

        let typeCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("type"))
        typeCol.title = "Type"
        typeCol.width = 55
        typeCol.minWidth = 45
        typeCol.maxWidth = 70
        tableView.addTableColumn(typeCol)

        let statusCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        statusCol.title = "Status"
        statusCol.width = 80
        statusCol.minWidth = 60
        tableView.addTableColumn(statusCol)

        tableView.dataSource = self
        tableView.delegate = self
        scrollView.documentView = tableView

        summaryLabel = makeLabel("", size: 11, color: .tertiaryLabelColor)
        content.addSubview(summaryLabel)

        // -- Progress area --
        progressBar = NSProgressIndicator()
        progressBar.translatesAutoresizingMaskIntoConstraints = false
        progressBar.isIndeterminate = false
        progressBar.minValue = 0
        progressBar.maxValue = 100
        progressBar.doubleValue = 0
        progressBar.style = .bar
        progressBar.isHidden = true
        content.addSubview(progressBar)

        progressLabel = makeLabel("", size: 11, color: .secondaryLabelColor)
        progressLabel.isHidden = true
        content.addSubview(progressLabel)

        // -- Action buttons --
        let buttonBar = NSView()
        buttonBar.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(buttonBar)

        refreshButton = makeButton("Refresh", action: #selector(refreshTapped))
        buttonBar.addSubview(refreshButton)

        downloadButton = makeButton("Download Selected", action: #selector(downloadTapped))
        downloadButton.bezelColor = HumDropColors.accent
        buttonBar.addSubview(downloadButton)

        openFolderButton = makeButton("Open Folder", action: #selector(openFolderTapped))
        buttonBar.addSubview(openFolderButton)

        let sep2 = NSBox()
        sep2.boxType = .separator
        sep2.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(sep2)

        // -- Naming config row --
        let namingTitle = makeLabel("Naming", size: 11, bold: true)
        content.addSubview(namingTitle)

        namingPopup = NSPopUpButton()
        namingPopup.translatesAutoresizingMaskIntoConstraints = false
        namingPopup.controlSize = .small
        namingPopup.font = NSFont.systemFont(ofSize: 10)
        updateNamingPopupItems()
        if let idx = NamingScheme.allCases.firstIndex(of: camera.namingScheme) {
            namingPopup.selectItem(at: idx)
        }
        namingPopup.target = self
        namingPopup.action = #selector(namingSchemeChanged)
        content.addSubview(namingPopup)

        prefixLabel = makeLabel("Prefix:", size: 11, color: .secondaryLabelColor)
        content.addSubview(prefixLabel)

        prefixField = NSTextField()
        prefixField.translatesAutoresizingMaskIntoConstraints = false
        prefixField.stringValue = camera.namingPrefix
        prefixField.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        prefixField.isBordered = true
        prefixField.isBezeled = true
        prefixField.bezelStyle = .roundedBezel
        prefixField.isEditable = true
        prefixField.placeholderString = "e.g. hummingbird"
        prefixField.target = self
        prefixField.action = #selector(prefixChanged)
        content.addSubview(prefixField)

        exampleLabel = makeLabel("", size: 10, color: .tertiaryLabelColor)
        exampleLabel.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        content.addSubview(exampleLabel)
        updateExampleLabel()

        // -- Bottom buttons --
        cleanButton = makeButton("Delete Downloaded from Camera", action: #selector(cleanTapped))
        content.addSubview(cleanButton)

        wipeButton = makeButton("Wipe All Camera Files", action: #selector(wipeTapped))
        content.addSubview(wipeButton)

        // Store references for show/hide
        fileSectionViews = [fileHeaderLabel, selectAllButton, selectNewButton, scrollView, summaryLabel, progressBar, progressLabel, buttonBar, sep2, namingTitle, namingPopup, prefixLabel, prefixField, exampleLabel, cleanButton, wipeButton]

        NSLayoutConstraint.activate([
            fileHeaderLabel.topAnchor.constraint(equalTo: headerView.bottomAnchor, constant: 12),
            fileHeaderLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),

            selectNewButton.centerYAnchor.constraint(equalTo: fileHeaderLabel.centerYAnchor),
            selectNewButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),

            selectAllButton.centerYAnchor.constraint(equalTo: fileHeaderLabel.centerYAnchor),
            selectAllButton.trailingAnchor.constraint(equalTo: selectNewButton.leadingAnchor, constant: -8),

            scrollView.topAnchor.constraint(equalTo: fileHeaderLabel.bottomAnchor, constant: 8),
            scrollView.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            scrollView.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            scrollView.bottomAnchor.constraint(equalTo: summaryLabel.topAnchor, constant: -6),

            summaryLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            summaryLabel.bottomAnchor.constraint(equalTo: progressBar.topAnchor, constant: -8),

            progressBar.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            progressBar.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            progressBar.bottomAnchor.constraint(equalTo: progressLabel.topAnchor, constant: -4),
            progressBar.heightAnchor.constraint(equalToConstant: 6),

            progressLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            progressLabel.bottomAnchor.constraint(equalTo: buttonBar.topAnchor, constant: -8),

            buttonBar.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            buttonBar.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            buttonBar.heightAnchor.constraint(equalToConstant: 32),
            buttonBar.bottomAnchor.constraint(equalTo: sep2.topAnchor, constant: -12),

            refreshButton.leadingAnchor.constraint(equalTo: buttonBar.leadingAnchor),
            refreshButton.centerYAnchor.constraint(equalTo: buttonBar.centerYAnchor),

            downloadButton.centerXAnchor.constraint(equalTo: buttonBar.centerXAnchor),
            downloadButton.centerYAnchor.constraint(equalTo: buttonBar.centerYAnchor),

            openFolderButton.trailingAnchor.constraint(equalTo: buttonBar.trailingAnchor),
            openFolderButton.centerYAnchor.constraint(equalTo: buttonBar.centerYAnchor),

            sep2.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            sep2.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            sep2.bottomAnchor.constraint(equalTo: namingTitle.topAnchor, constant: -8),

            namingTitle.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            namingTitle.bottomAnchor.constraint(equalTo: prefixLabel.topAnchor, constant: -6),

            namingPopup.centerYAnchor.constraint(equalTo: namingTitle.centerYAnchor),
            namingPopup.leadingAnchor.constraint(equalTo: namingTitle.trailingAnchor, constant: 8),

            prefixLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            prefixLabel.bottomAnchor.constraint(equalTo: cleanButton.topAnchor, constant: -10),

            prefixField.centerYAnchor.constraint(equalTo: prefixLabel.centerYAnchor),
            prefixField.leadingAnchor.constraint(equalTo: prefixLabel.trailingAnchor, constant: 4),
            prefixField.widthAnchor.constraint(equalToConstant: 140),

            exampleLabel.centerYAnchor.constraint(equalTo: prefixLabel.centerYAnchor),
            exampleLabel.leadingAnchor.constraint(equalTo: prefixField.trailingAnchor, constant: 8),

            cleanButton.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            cleanButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),

            wipeButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            wipeButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
        ])
    }

    func updateNamingPopupItems() {
        namingPopup?.removeAllItems()
        for scheme in NamingScheme.allCases {
            namingPopup?.addItem(withTitle: "\(scheme.displayName)  (\(scheme.example(prefix: camera.namingPrefix)))")
            namingPopup?.lastItem?.representedObject = scheme.rawValue
        }
    }

    func updateExampleLabel() {
        let example = camera.namingScheme.example(prefix: camera.namingPrefix)
        exampleLabel?.stringValue = "e.g. \(example)"
    }

    func updateInstructionText() {
        instructionLabel.stringValue = """
        To get started:

        1.  Open the camera app on your phone
        2.  Start the camera stream
        3.  Make sure you're on the same Wi-Fi network
        4.  Enter the camera IP, or tap "Find" to auto-discover, then "Connect"
        """
    }

    // MARK: - State Management

    func updateConnectionStatus(connected: Bool) {
        isConnected = connected

        DispatchQueue.main.async { [self] in
            statusDot.layer?.backgroundColor = connected
                ? HumDropColors.connected.cgColor
                : HumDropColors.disconnected.cgColor
            statusLabel.stringValue = connected ? "Connected" : "Disconnected"
            connectButton.title = connected ? "Disconnect" : "Connect"

            instructionView.isHidden = connected
            for view in fileSectionViews {
                view.isHidden = !connected
            }
        }
    }

    func updateSummary() {
        let total = files.count
        let downloaded = files.filter { $0.isDownloaded }.count
        let newCount = total - downloaded
        let selected = files.filter { $0.selected }.count
        let videos = files.filter { $0.isVideo }.count
        let photos = total - videos
        let totalBytes = files.reduce(Int64(0)) { $0 + $1.sizeBytes }
        let totalMB = Double(totalBytes) / 1_048_576.0
        let sizeStr = totalMB >= 1000 ? String(format: "%.1f GB", totalMB / 1024.0) : String(format: "%.0f MB", totalMB)
        summaryLabel.stringValue = "\(total) files (\(videos) vid, \(photos) photo, \(sizeStr))  \u{2022}  \(downloaded) synced  \u{2022}  \(newCount) new  \u{2022}  \(selected) selected"
    }

    // MARK: - Actions

    @objc func connectTapped() {
        if isConnected {
            camera.cleanup()
            updateConnectionStatus(connected: false)
            files = []
            tableView.reloadData()
            return
        }

        ipChanged()

        connectButton.isEnabled = false
        statusLabel.stringValue = "Connecting..."

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let reachable = camera.isCameraReachable()

            if !reachable {
                DispatchQueue.main.async {
                    self.connectButton.isEnabled = true
                    self.statusLabel.stringValue = "Not found"

                    let alert = NSAlert()
                    alert.messageText = "Camera Not Found"
                    alert.informativeText = "Could not reach the camera at \(self.camera.cameraIP).\n\nMake sure:\n1. The camera app is open and streaming\n2. You're on the same Wi-Fi network\n3. The camera is powered on\n\nYou can edit the IP address in the header."
                    alert.alertStyle = .warning
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
                return
            }

            DispatchQueue.main.async {
                self.statusLabel.stringValue = "Starting camera server..."
            }

            camera.startHTTPD()

            let httpOK = camera.isHTTPDRunning()

            DispatchQueue.main.async {
                self.connectButton.isEnabled = true
                if httpOK {
                    self.updateConnectionStatus(connected: true)
                    self.refreshTapped()
                } else {
                    self.updateConnectionStatus(connected: true)
                    self.statusLabel.stringValue = "Connected (HTTP may be slow)"
                    self.refreshTapped()
                }
            }
        }
    }

    @objc func refreshTapped() {
        guard isConnected else { return }
        refreshButton.isEnabled = false
        statusLabel.stringValue = "Scanning..."

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let foundFiles = camera.listFiles()

            DispatchQueue.main.async {
                self.files = foundFiles
                self.tableView.reloadData()
                self.updateSummary()
                self.refreshButton.isEnabled = true
                self.statusLabel.stringValue = foundFiles.isEmpty
                    ? "Connected (no files found)"
                    : "Connected"
            }
        }
    }

    @objc func downloadTapped() {
        guard !isDownloading else { return }

        let selected = files.enumerated().filter { $0.element.selected }
        guard !selected.isEmpty else {
            let total = files.count
            let downloaded = files.filter { $0.isDownloaded }.count
            let alert = NSAlert()
            alert.messageText = "Nothing Selected"
            if total == 0 {
                alert.informativeText = "No files were found on the camera. Try clicking Refresh."
            } else if downloaded == total {
                alert.informativeText = "All \(total) files are already synced. Click \"Select All\" to re-download, or check individual files in the list."
            } else {
                alert.informativeText = "Select files to download by checking the boxes in the list."
            }
            alert.runModal()
            return
        }

        isDownloading = true
        downloadButton.isEnabled = false
        refreshButton.isEnabled = false
        cleanButton.isEnabled = false
        wipeButton.isEnabled = false
        progressBar.isHidden = false
        progressLabel.isHidden = false
        progressBar.doubleValue = 0

        let total = selected.count
        var completed = 0

        func downloadNext(_ index: Int) {
            guard index < selected.count else {
                DispatchQueue.main.async { [self] in
                    isDownloading = false
                    downloadButton.isEnabled = true
                    refreshButton.isEnabled = true
                    cleanButton.isEnabled = true
                    wipeButton.isEnabled = true
                    progressLabel.stringValue = "Done! Downloaded \(total) files."
                    progressBar.doubleValue = 100
                    tableView.reloadData()
                    updateSummary()

                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [self] in
                        progressBar.isHidden = true
                        progressLabel.isHidden = true
                    }
                }
                return
            }

            let item = selected[index]
            DispatchQueue.main.async { [self] in
                let dlName = item.element.isRenamed
                    ? "\(item.element.name) -> \(item.element.localName)"
                    : item.element.localName
                progressLabel.stringValue = "Downloading \(dlName) (\(completed + 1)/\(total))..."
            }

            camera.downloadFile(item.element, progress: { pct in
                DispatchQueue.main.async { [self] in
                    let overall = (Double(completed) + pct) / Double(total) * 100
                    progressBar.doubleValue = overall
                }
            }, completion: { [self] success in
                completed += 1
                if success {
                    DispatchQueue.main.async {
                        self.files[item.offset].isDownloaded = true
                        self.files[item.offset].selected = false
                        self.tableView.reloadData()
                    }
                }
                downloadNext(index + 1)
            })
        }

        downloadNext(0)
    }

    @objc func cleanTapped() {
        let downloaded = files.filter { $0.isDownloaded }
        guard !downloaded.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Nothing to Clean"
            alert.informativeText = "No downloaded files to remove from camera."
            alert.runModal()
            return
        }

        let alert = NSAlert()
        alert.messageText = "Delete Downloaded Files from Camera?"
        alert.informativeText = "This will delete \(downloaded.count) files from the camera that have already been downloaded to your Mac."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete")
        alert.addButton(withTitle: "Cancel")

        guard alert.runModal() == .alertFirstButtonReturn else { return }

        cleanButton.isEnabled = false
        statusLabel.stringValue = "Cleaning..."

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            for file in downloaded {
                camera.deleteFile(file)
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async { [self] in
                cleanButton.isEnabled = true
                statusLabel.stringValue = "Connected"
                refreshTapped()
            }
        }
    }

    @objc func wipeTapped() {
        let alert = NSAlert()
        alert.messageText = "Wipe ALL Files from Camera?"
        alert.informativeText = "This will permanently delete ALL videos and photos from the camera. This cannot be undone."
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Wipe Everything")
        alert.addButton(withTitle: "Cancel")

        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let confirm = NSAlert()
        confirm.messageText = "Are you sure?"
        confirm.informativeText = "All camera files will be permanently deleted."
        confirm.alertStyle = .critical
        confirm.addButton(withTitle: "Yes, Wipe All")
        confirm.addButton(withTitle: "Cancel")

        guard confirm.runModal() == .alertFirstButtonReturn else { return }

        wipeButton.isEnabled = false
        statusLabel.stringValue = "Wiping..."

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            camera.wipeAll()
            DispatchQueue.main.async { [self] in
                wipeButton.isEnabled = true
                statusLabel.stringValue = "Connected"
                refreshTapped()
            }
        }
    }

    @objc func namingSchemeChanged() {
        let idx = namingPopup.indexOfSelectedItem
        guard idx >= 0, idx < NamingScheme.allCases.count else { return }
        camera.namingScheme = NamingScheme.allCases[idx]
        updateExampleLabel()

        // Hide prefix field for "original camera" scheme
        let isOriginal = camera.namingScheme == .originalCamera
        prefixLabel.isHidden = isOriginal
        prefixField.isHidden = isOriginal
        exampleLabel.isHidden = isOriginal

        if !files.isEmpty && isConnected {
            refreshTapped()
        }
    }

    @objc func prefixChanged() {
        var newPrefix = prefixField.stringValue.trimmingCharacters(in: .whitespaces)
        // Sanitize: lowercase, replace spaces with underscores, remove unsafe chars
        newPrefix = newPrefix.lowercased()
            .replacingOccurrences(of: " ", with: "_")
            .filter { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" }
        if newPrefix.isEmpty { newPrefix = "cam" }
        prefixField.stringValue = newPrefix
        camera.namingPrefix = newPrefix
        updateNamingPopupItems()
        if let idx = NamingScheme.allCases.firstIndex(of: camera.namingScheme) {
            namingPopup.selectItem(at: idx)
        }
        updateExampleLabel()

        if !files.isEmpty && isConnected {
            refreshTapped()
        }
    }

    @objc func openFolderTapped() {
        NSWorkspace.shared.open(camera.videoDir)
    }

    @objc func selectAllFiles() {
        for i in 0..<files.count {
            files[i].selected = true
        }
        tableView.reloadData()
        updateSummary()
    }

    @objc func selectAllNew() {
        for i in 0..<files.count {
            files[i].selected = !files[i].isDownloaded
        }
        tableView.reloadData()
        updateSummary()
    }

    @objc func checkboxToggled(_ sender: NSButton) {
        let row = sender.tag
        guard row >= 0 && row < files.count else { return }
        files[row].selected = sender.state == .on
        updateSummary()
    }

    @objc func ipChanged() {
        var newIP = ipField.stringValue.trimmingCharacters(in: .whitespaces)
        guard !newIP.isEmpty else { return }

        if let num = Int(newIP), num >= 0, num <= 255 {
            newIP = "192.168.1.\(num)"
            ipField.stringValue = newIP
        }

        camera.cameraIP = newIP

        if isConnected {
            camera.cleanup()
            updateConnectionStatus(connected: false)
            files = []
            tableView.reloadData()
        }
    }

    @objc func findCameraTapped() {
        findButton.isEnabled = false
        statusLabel.stringValue = "Scanning network..."

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            var foundIP: String? = nil
            var method = ""

            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/sbin/arp")
            process.arguments = ["-a"]
            let pipe = Pipe()
            process.standardOutput = pipe
            if let _ = try? process.run() {
                process.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                let output = String(data: data, encoding: .utf8) ?? ""
                let lines = output.components(separatedBy: "\n")
                for line in lines {
                    if line.lowercased().contains("50:5a:65") {
                        if let regex = try? NSRegularExpression(pattern: "\\(([\\d.]+)\\)"),
                           let match = regex.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
                           let range = Range(match.range(at: 1), in: line) {
                            foundIP = String(line[range])
                            method = "MAC address"
                            break
                        }
                    }
                }
            }

            if foundIP == nil {
                var candidates: [String] = []
                let p2 = Process()
                p2.executableURL = URL(fileURLWithPath: "/usr/sbin/arp")
                p2.arguments = ["-a"]
                let pipe2 = Pipe()
                p2.standardOutput = pipe2
                if let _ = try? p2.run() {
                    p2.waitUntilExit()
                    let data = pipe2.fileHandleForReading.readDataToEndOfFile()
                    let output = String(data: data, encoding: .utf8) ?? ""
                    if let regex = try? NSRegularExpression(pattern: "\\(([\\d.]+)\\)") {
                        let matches = regex.matches(in: output, range: NSRange(output.startIndex..., in: output))
                        for match in matches {
                            if let range = Range(match.range(at: 1), in: output) {
                                candidates.append(String(output[range]))
                            }
                        }
                    }
                }

                for last in [83, 84, 85, 80, 81, 82, 86, 87, 88, 89, 90] {
                    let ip = "192.168.1.\(last)"
                    if !candidates.contains(ip) { candidates.append(ip) }
                }

                let group = DispatchGroup()
                let lock = NSLock()
                for ip in candidates {
                    group.enter()
                    DispatchQueue.global().async {
                        defer { group.leave() }
                        var inS: InputStream?
                        var outS: OutputStream?
                        Stream.getStreamsToHost(withName: ip, port: 23, inputStream: &inS, outputStream: &outS)
                        guard let input = inS, let output = outS else { return }
                        input.open()
                        output.open()
                        let deadline = Date().addingTimeInterval(0.8)
                        while output.streamStatus != .open && Date() < deadline {
                            Thread.sleep(forTimeInterval: 0.05)
                        }
                        let telnetOpen = output.streamStatus == .open
                        input.close()
                        output.close()

                        if telnetOpen {
                            var inS2: InputStream?
                            var outS2: OutputStream?
                            Stream.getStreamsToHost(withName: ip, port: 8080, inputStream: &inS2, outputStream: &outS2)
                            guard let in2 = inS2, let out2 = outS2 else { return }
                            in2.open()
                            out2.open()
                            let deadline2 = Date().addingTimeInterval(0.8)
                            while out2.streamStatus != .open && Date() < deadline2 {
                                Thread.sleep(forTimeInterval: 0.05)
                            }
                            let httpOpen = out2.streamStatus == .open
                            in2.close()
                            out2.close()

                            if httpOpen {
                                lock.lock()
                                if foundIP == nil {
                                    foundIP = ip
                                    method = "port scan (telnet+HTTP)"
                                }
                                lock.unlock()
                            }
                        }
                    }
                }
                group.wait()
            }

            DispatchQueue.main.async { [self] in
                findButton.isEnabled = true
                statusLabel.stringValue = "Disconnected"

                if let ip = foundIP {
                    ipField.stringValue = ip
                    camera.cameraIP = ip

                    let alert = NSAlert()
                    alert.messageText = "Camera Found!"
                    alert.informativeText = "Found camera at \(ip)\n(Detected via \(method))"
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                } else {
                    let alert = NSAlert()
                    alert.messageText = "Camera Not Found"
                    alert.informativeText = "No camera found on your network.\n\nMake sure:\n1. The camera app is open and streaming\n2. You're on the same Wi-Fi network\n\nManual method:\n1. Open Terminal\n2. Run:  arp -a\n3. Look for your camera's MAC address"
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
            }
        }
    }

    @objc func changeFolderTapped() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = camera.videoDir
        panel.prompt = "Choose"
        panel.message = "Select download folder"

        guard panel.runModal() == .OK, let url = panel.url else { return }

        camera.changeVideoDir(to: url)
        updateFolderDisplay()
    }

    func updateFolderDisplay() {
        folderPathControl.url = camera.videoDir
    }

    @objc func showAbout() {
        let alert = NSAlert()
        alert.messageText = "HumDrop"
        alert.informativeText = "v0.05\nBy Kenneth Russell DeGraff\n\nSync videos and photos from your camera.\n\nDownloads to:\n\(camera.videoDir.path)\n\nCamera IP: \(camera.cameraIP)"
        alert.icon = createAppIcon()
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    // MARK: - Helpers

    func makeLabel(_ text: String, size: CGFloat, bold: Bool = false, color: NSColor = .labelColor) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.translatesAutoresizingMaskIntoConstraints = false
        label.font = bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.systemFont(ofSize: size)
        label.textColor = color
        label.isSelectable = false
        return label
    }

    func makeButton(_ title: String, action: Selector) -> NSButton {
        let btn = NSButton(title: title, target: self, action: action)
        btn.translatesAutoresizingMaskIntoConstraints = false
        btn.bezelStyle = .rounded
        btn.controlSize = .regular
        return btn
    }
}

// MARK: - Table View Data Source & Delegate

extension MainWindowController: NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int {
        return files.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < files.count else { return nil }
        let file = files[row]
        let colID = tableColumn?.identifier.rawValue ?? ""

        switch colID {
        case "check":
            let check = NSButton(checkboxWithTitle: "", target: self, action: #selector(checkboxToggled(_:)))
            check.state = file.selected ? .on : .off
            check.tag = row
            return check

        case "camera":
            let cell = NSTextField(labelWithString: file.name)
            cell.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
            cell.textColor = .secondaryLabelColor
            cell.lineBreakMode = .byTruncatingTail
            return cell

        case "saveas":
            let cell = NSTextField(labelWithString: file.isRenamed ? file.localName : "—")
            cell.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
            cell.textColor = file.isRenamed ? .labelColor : .tertiaryLabelColor
            cell.lineBreakMode = .byTruncatingTail
            return cell

        case "size":
            let cell = NSTextField(labelWithString: file.sizeString)
            cell.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
            cell.textColor = .secondaryLabelColor
            cell.alignment = .right
            return cell

        case "type":
            let cell = NSTextField(labelWithString: file.isVideo ? "Video" : "Photo")
            cell.font = NSFont.systemFont(ofSize: 12, weight: .medium)
            cell.textColor = file.isVideo ? .systemBlue : .systemOrange
            return cell

        case "status":
            let text: String
            let color: NSColor
            let weight: NSFont.Weight
            if file.isDownloaded {
                text = "Synced"
                color = .tertiaryLabelColor
                weight = .regular
            } else {
                text = "New"
                color = HumDropColors.newFile
                weight = .semibold
            }
            let cell = NSTextField(labelWithString: text)
            cell.font = NSFont.systemFont(ofSize: 12, weight: weight)
            cell.textColor = color
            return cell

        default:
            return nil
        }
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    var windowController: MainWindowController!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = createAppIcon()

        windowController = MainWindowController()
        windowController.showWindow(nil)
        windowController.window?.makeKeyAndOrderFront(nil)

        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About HumDrop", action: #selector(windowController.showAbout), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit HumDrop", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        let fileMenuItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Refresh", action: #selector(windowController.refreshTapped), keyEquivalent: "r")
        fileMenu.addItem(withTitle: "Open Download Folder", action: #selector(windowController.openFolderTapped), keyEquivalent: "o")
        fileMenuItem.submenu = fileMenu
        mainMenu.addItem(fileMenuItem)

        NSApp.mainMenu = mainMenu
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        CameraManager.shared.cleanup()
    }
}

// MARK: - isHTTPDRunning

extension CameraManager {
    func isHTTPDRunning() -> Bool {
        let sem = DispatchSemaphore(value: 0)
        var reachable = false
        let url = URL(string: "http://\(cameraIP):\(httpPort)/")!
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        req.httpMethod = "HEAD"
        let task = URLSession.shared.dataTask(with: req) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode < 500 {
                reachable = true
            }
            sem.signal()
        }
        task.resume()
        sem.wait()
        return reachable
    }
}

// MARK: - Entry Point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
