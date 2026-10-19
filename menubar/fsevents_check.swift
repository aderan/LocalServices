// fsevents-check DIR: exit 0 if a file written into DIR is reported by FSEvents within 3s, else exit 1.
// Dev-server hot reload (Vite/Next/Turbopack) depends on FSEvents; the daemon uses this as a canary.
import CoreServices
import Foundation

let dir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : NSTemporaryDirectory()
let real = (dir as NSString).resolvingSymlinksInPath
var seen = 0
let cb: FSEventStreamCallback = { _, _, n, _, _, _ in seen += Int(n) }
var ctx = FSEventStreamContext()
guard let stream = FSEventStreamCreate(nil, cb, &ctx, [real] as CFArray,
                                       FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.05,
                                       FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents)) else { exit(2) }
FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
FSEventStreamStart(stream)
let probe = real + "/.fsevents-canary"
DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
    try? "\(Date().timeIntervalSince1970)".write(toFile: probe, atomically: false, encoding: .utf8)
}
var waited = 0.0
func tick() {
    if seen > 0 { try? FileManager.default.removeItem(atPath: probe); print("ok"); exit(0) }
    waited += 0.1
    if waited > 3 { try? FileManager.default.removeItem(atPath: probe); print("no events"); exit(1) }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: tick)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: tick)
dispatchMain()
