import SwiftUI
import AppKit

struct Backend: Decodable {
    let python: String
    let root: String
    let config: String
    let environment: [String: String]

    func run(_ arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        process.environment = environment
        process.arguments = ["-B", "-m", "apee", "--config", config] + arguments
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        DispatchQueue.global().asyncAfter(deadline: .now() + 90) {
            if process.isRunning { process.terminate() }
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw NSError(domain: "APEE", code: Int(process.terminationStatus), userInfo: [
                NSLocalizedDescriptionKey: String(data: data, encoding: .utf8) ?? "操作失敗"
            ])
        }
        return data
    }
}

final class DashboardModel: ObservableObject {
    @Published var data: [String: Any] = [:]
    @Published var busy = false
    @Published var message = ""
    @Published var error = false
    private let backend: Backend?

    init() {
        backend = Bundle.main.url(forResource: "backend", withExtension: "json")
            .flatMap { try? Data(contentsOf: $0) }
            .flatMap { try? JSONDecoder().decode(Backend.self, from: $0) }
        refresh()
    }

    var tasks: [[String: Any]] { data["tasks"] as? [[String: Any]] ?? [] }
    var budget: [String: Any] { data["budget"] as? [String: Any] ?? [:] }
    var service: [String: Any] { data["service"] as? [String: Any] ?? [:] }
    var paused: Bool { data["paused"] is [String: Any] }

    func command(_ args: [String]) {
        guard !busy else { return }
        perform(args, snapshot: false)
    }

    func refresh() {
        guard !busy else { return }
        perform(["snapshot"], snapshot: true)
    }

    private func perform(_ args: [String], snapshot: Bool) {
        guard let backend = backend else {
            message = "無法讀取這份 app 的執行設定。"
            error = true
            return
        }
        busy = true
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try backend.run(args)
                let object = snapshot ? try JSONSerialization.jsonObject(with: result) as? [String: Any] : nil
                DispatchQueue.main.async {
                    self.busy = false
                    if snapshot {
                        if let object = object { self.data = object }
                        else { self.message = "無法辨識狀態資料。"; self.error = true }
                    } else {
                        self.message = "設定已套用。"
                        self.error = false
                        self.refresh()
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.busy = false
                    self.message = error.localizedDescription
                    self.error = true
                }
            }
        }
    }

    func save(_ settings: [String: Any]) {
        do {
            let data = try JSONSerialization.data(withJSONObject: settings, options: [.sortedKeys])
            command(["configure", "--settings-json", String(decoding: data, as: UTF8.self)])
        } catch {
            message = error.localizedDescription
            self.error = true
        }
    }
}

func value(_ dictionary: [String: Any], _ key: String, fallback: String = "—") -> String {
    guard let item = dictionary[key], !(item is NSNull) else { return fallback }
    return String(describing: item)
}

struct StatCard: View {
    let label: String
    let number: String
    let icon: String
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(label, systemImage: icon).foregroundStyle(.secondary)
            Text(number).font(.system(size: 25, weight: .semibold, design: .rounded))
        }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
            .background(.background, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct Dashboard: View {
    @StateObject private var model = DashboardModel()
    @State private var tab = "工作總覽"
    private let timer = Timer.publish(every: 10, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 14) {
                Image(systemName: "arrow.triangle.branch").font(.title).foregroundStyle(.teal)
                    .frame(width: 52, height: 52).background(.teal.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
                VStack(alignment: .leading, spacing: 3) {
                    Text("APEE").font(.largeTitle.bold())
                    Text("把注意力留給方向與成果").foregroundStyle(.secondary)
                }
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("重新整理", systemImage: "arrow.clockwise") { model.refresh() }
                Button(model.paused ? "恢復派工" : "暫停派工", systemImage: model.paused ? "play.fill" : "pause.fill") {
                    model.command(model.paused ? ["resume"] : ["pause", "--reason", "桌面介面暫停派工。"])
                }.buttonStyle(.borderedProminent).tint(.teal)
            }
            HStack(spacing: 12) {
                StatCard(label: "等待開發", number: String(model.tasks.filter { $0["state"] as? String == "queued" }.count), icon: "tray")
                StatCard(label: "今日嘗試", number: value(model.data["attempts"] as? [String: Any] ?? [:], "used") + " / " + value(model.data, "daily_attempt_limit"), icon: "play.circle")
                StatCard(label: "今日已核對 token", number: value(model.budget, "tokens"), icon: "chart.bar")
                StatCard(label: "背景排程", number: (model.service["loaded"] as? Bool).map { $0 ? "已啟用" : "未啟用" } ?? "狀態不明", icon: "clock")
            }
            if !model.message.isEmpty {
                HStack(alignment: .top) {
                    Image(systemName: model.error ? "exclamationmark.circle" : "checkmark.circle")
                    Text(model.message).textSelection(.enabled)
                    Spacer()
                    Button { model.message = "" } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                }.foregroundStyle(model.error ? .red : .secondary).padding(12)
                    .background(.background, in: RoundedRectangle(cornerRadius: 10))
            }
            Picker("檢視", selection: $tab) {
                Text("工作總覽").tag("工作總覽")
                Text("用量與預算").tag("用量與預算")
                Text("恢復政策").tag("恢復政策")
            }.pickerStyle(.segmented).frame(width: 440)
            Group {
                if tab == "用量與預算" { BudgetPanel(model: model) }
                else if tab == "恢復政策" { RecoveryPanel(model: model) }
                else { overview }
            }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            HStack {
                Text("v" + value(model.data, "version") + " · " + value(model.data, "at") + " · 狀態以本機記錄為準")
                Spacer()
                Button("開啟總覽") {
                    if let path = model.data["overview"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                }
            }.font(.caption).foregroundStyle(.secondary)
        }.padding(26).background(Color(nsColor: .windowBackgroundColor))
            .frame(minWidth: 1040, minHeight: 700)
            .disabled(model.busy && model.data.isEmpty)
            .onReceive(timer) { _ in model.refresh() }
            .onAppear { NSApp.activate(ignoringOtherApps: true) }
    }

    var overview: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label(model.paused ? "派工已暫停，當前任務仍可完成" : "依佇列與專案優先順序推進", systemImage: "circle.fill")
                    .foregroundStyle(model.paused ? .orange : .teal)
                Spacer()
                Button("更新 GitHub 近況") { model.command(["refresh"]) }.disabled(model.busy)
                Button(model.service["loaded"] as? Bool == true ? "停止背景排程" : "啟動背景排程") {
                    model.command(["service", model.service["loaded"] as? Bool == true ? "stop" : "start"])
                }.disabled(model.busy)
            }
            if let blocker = model.data["budget_blocker"] as? [String: Any] {
                Label(value(blocker, "message"), systemImage: "exclamationmark.circle").foregroundStyle(.orange)
            }
            if let hold = model.data["codex_hold"] as? [String: Any] {
                Text(value(hold, "message")).foregroundStyle(.orange)
            }
            ScrollView {
                LazyVStack(spacing: 10) {
                    if model.tasks.isEmpty {
                        VStack(spacing: 12) {
                            Image(systemName: "tray").font(.system(size: 32)).foregroundStyle(.secondary)
                            Text("尚無開發任務").font(.title3.bold())
                            Text("加入具體任務，或在已啟用專案的 Issue 加上派工標籤。")
                                .foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity).padding(50)
                    }
                    ForEach(Array(model.tasks.enumerated()), id: \.offset) { _, task in
                        HStack(alignment: .top, spacing: 18) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(value(task, "repo")).font(.caption).foregroundStyle(.secondary)
                                Text(value(task, "title")).font(.headline)
                                Text(value(task, "state_label")).foregroundStyle(.teal)
                                if let summary = task["pr_summary"] as? String { Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                                if let error = task["error"] as? String { Text(error).font(.caption).foregroundStyle(.secondary).lineLimit(3) }
                            }
                            Spacer()
                            if let link = task["pr_url"] as? String, let url = URL(string: link), url.scheme == "https", url.host == "github.com" {
                                Button("查看 PR") { NSWorkspace.shared.open(url) }
                            }
                            if ["failed", "interrupted", "waiting"].contains(value(task, "state")) {
                                Button("重新入列") { model.command(["retry", value(task, "id")]) }.disabled(model.busy)
                            }
                            if ["preparing", "running", "testing"].contains(value(task, "state")) {
                                Button("核對恢復") { model.command(["recover", value(task, "id")]) }.disabled(model.busy)
                            }
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.background, in: RoundedRectangle(cornerRadius: 12))
                    }
                }
            }
        }
    }
}

struct AuditSelection: Identifiable {
    let record: [String: Any]
    var id: String { value(record, "run_id") }
}

struct BudgetPanel: View {
    @ObservedObject var model: DashboardModel
    @State private var tokens = ""
    @State private var dollars = ""
    @State private var modelName = ""
    @State private var input = ""
    @State private var cached = ""
    @State private var output = ""
    @State private var auditSelection: AuditSelection?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("預算控制新的模型派工").font(.title2.bold())
                Text(value(model.budget, "notice")).foregroundStyle(.secondary)
                HStack(spacing: 24) {
                    Text("輸入 " + value(model.budget, "input_tokens"))
                    Text("其中快取 " + value(model.budget, "cached_input_tokens"))
                    Text("輸出 " + value(model.budget, "output_tokens"))
                    Text("估算 USD " + value(model.budget, "estimated_usd", fallback: "未定價"))
                }.font(.callout.monospacedDigit())
                let unknown = model.budget["unknown_runs"] as? [String] ?? []
                let unpriced = model.budget["unpriced_runs"] as? [String] ?? []
                if !unknown.isEmpty || !unpriced.isEmpty {
                    Text("用量待核對 \(unknown.count) 回合；費用未定價 \(unpriced.count) 回合。啟用相關預算後，這些回合會阻擋新派工。").foregroundStyle(.orange)
                }
                Grid(alignment: .leading, horizontalSpacing: 22, verticalSpacing: 14) {
                    GridRow { Text("每日 token 預算"); TextField("留空表示未設定", text: $tokens) }
                    GridRow { Text("每日估算 USD 預算"); TextField("留空表示未設定", text: $dollars) }
                    GridRow { Text("明確模型名稱"); TextField("費用估算必填；留空沿用 CLI 預設", text: $modelName) }
                    GridRow { Text("每百萬輸入 token / USD"); TextField("明確單價", text: $input) }
                    GridRow { Text("每百萬快取輸入 / USD"); TextField("明確單價", text: $cached) }
                    GridRow { Text("每百萬輸出 token / USD"); TextField("明確單價", text: $output) }
                }.textFieldStyle(.roundedBorder).frame(maxWidth: 680)
                Text("按模型回合開始的 UTC 日期計算。這是 APEE 已記錄用量，不代表整個帳號的訂閱額度、credits 或實際帳單。").font(.callout).foregroundStyle(.secondary)
                HStack {
                    Button("儲存預算") { save() }.buttonStyle(.borderedProminent).tint(.teal).disabled(model.busy)
                    Button("重新載入設定") { load() }
                }
                Divider()
                Text("今日模型回合").font(.headline)
                let records = model.budget["records"] as? [[String: Any]] ?? []
                ForEach(Array(records.enumerated()), id: \.offset) { _, record in
                    HStack {
                        Text(String(value(record, "run_id").prefix(12))).font(.callout.monospaced())
                        Text(value(record, "status") == "known" ? "用量已核對" : "用量待核對").foregroundStyle(.secondary)
                        Spacer()
                        Text("USD " + value(record, "estimated_usd", fallback: "未定價"))
                        Button("人工核對") { auditSelection = AuditSelection(record: record) }
                    }
                }
            }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
                .background(.background, in: RoundedRectangle(cornerRadius: 14))
        }.onAppear { load() }
            .sheet(item: $auditSelection) { selection in AuditPanel(model: model, record: selection.record) }
    }

    func load() {
        let limits = model.budget["limits"] as? [String: Any] ?? [:]
        let prices = limits["prices_per_million"] as? [String: Any] ?? [:]
        tokens = value(limits, "daily_tokens", fallback: "")
        dollars = value(limits, "daily_estimated_usd", fallback: "")
        modelName = value(model.data, "model", fallback: "")
        input = value(prices, "input", fallback: "")
        cached = value(prices, "cached_input", fallback: "")
        output = value(prices, "output", fallback: "")
    }

    func save() {
        var budget: [String: Any] = [:]
        if !tokens.isEmpty {
            guard let count = Int(tokens) else { model.message = "token 預算必須是整數。"; model.error = true; return }
            budget["daily_tokens"] = count
        }
        if !dollars.isEmpty { budget["daily_estimated_usd"] = dollars }
        if !input.isEmpty || !cached.isEmpty || !output.isEmpty {
            budget["prices_per_million"] = ["input": input, "cached_input": cached, "output": output]
        }
        model.save(["budget": budget, "model": modelName.isEmpty ? NSNull() : modelName as Any])
    }
}

struct AuditPanel: View {
    @ObservedObject var model: DashboardModel
    let record: [String: Any]
    @Environment(\.dismiss) private var dismiss
    @State private var input = ""
    @State private var cached = ""
    @State private var output = ""
    @State private var dollars = ""
    @State private var reason = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("人工核對模型回合").font(.title2.bold())
            Text(value(record, "run_id")).font(.caption.monospaced()).textSelection(.enabled)
            Text("已有可核對的 token 用量會保留；估算金額與核對原因將另存稽核紀錄。").foregroundStyle(.secondary)
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 12) {
                GridRow { Text("輸入 tokens"); TextField("必填", text: $input).disabled(record["status"] as? String == "known") }
                GridRow { Text("其中快取輸入"); TextField("必填", text: $cached).disabled(record["status"] as? String == "known") }
                GridRow { Text("輸出 tokens"); TextField("必填", text: $output).disabled(record["status"] as? String == "known") }
                GridRow { Text("估算 USD"); TextField("留空表示未定價", text: $dollars) }
                GridRow { Text("核對原因"); TextField("提供核對依據", text: $reason) }
            }.textFieldStyle(.roundedBorder)
            HStack {
                Button("開啟用量記錄") {
                    if let root = model.data["state_dir"] as? String {
                        let url = URL(fileURLWithPath: root).appendingPathComponent("runs").appendingPathComponent(value(record, "run_id")).appendingPathComponent("agent-events.log")
                        NSWorkspace.shared.open(url)
                    }
                }
                Spacer()
                Button("取消") { dismiss() }
                Button("保存核對紀錄") {
                    var args = ["budget", "record", value(record, "run_id"), "--input-tokens", input,
                                "--cached-input-tokens", cached, "--output-tokens", output, "--reason", reason]
                    if !dollars.isEmpty { args += ["--estimated-usd", dollars] }
                    model.command(args)
                    dismiss()
                }.buttonStyle(.borderedProminent).disabled(reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
            }
        }.padding(26).frame(width: 620)
            .onAppear {
                let usage = record["usage"] as? [String: Any] ?? [:]
                input = value(usage, "input_tokens", fallback: "")
                cached = value(usage, "cached_input_tokens", fallback: "")
                output = value(usage, "output_tokens", fallback: "")
                dollars = value(record, "estimated_usd", fallback: "")
            }
    }
}

struct RecoveryPanel: View {
    @ObservedObject var model: DashboardModel
    @State private var enabled = false
    @State private var retries = 1
    @State private var cooldown = 300
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("有上限的異常恢復").font(.title2.bold())
            Text("只處理來源明確的新版模型嘗試。確認程序鎖已釋放並經過觀察期後，保留原工作副本，以新的 checkout 重試。").foregroundStyle(.secondary)
            Toggle("啟用自動恢復", isOn: $enabled)
            Stepper("每項任務最多自動重試 \(retries) 次", value: $retries, in: 0...5)
            Stepper("連續停止觀察期 \(cooldown) 秒", value: $cooldown, in: 60...86400, step: 60)
            Text("0 次表示只標記中斷。派工暫停、Codex 暫停、每日上限與用量預算持續有效；舊版資料與交付結果不明的任務需人工核對。").font(.callout).foregroundStyle(.secondary)
            Button("儲存恢復政策") {
                model.save(["auto_recovery": ["enabled": enabled, "max_retries": retries, "cooldown_seconds": cooldown, "max_per_cycle": 1]])
            }.buttonStyle(.borderedProminent).tint(.teal).disabled(model.busy)
            let pending = model.data["recovery"] as? [[String: Any]] ?? []
            Divider()
            Text("需要核對的嘗試：\(pending.count)").font(.headline)
            ForEach(Array(pending.enumerated()), id: \.offset) { _, item in
                Text(value(item, "task") + " · " + value(item, "lease") + " · " + value(item, "action")).font(.callout)
            }
            Spacer()
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(.background, in: RoundedRectangle(cornerRadius: 14))
            .onAppear {
                let policy = model.data["auto_recovery"] as? [String: Any] ?? [:]
                enabled = policy["enabled"] as? Bool ?? false
                retries = policy["max_retries"] as? Int ?? 1
                cooldown = policy["cooldown_seconds"] as? Int ?? 300
            }
    }
}

@main
struct APEEApp: App {
    init() {
        if CommandLine.arguments.contains("--diagnose") {
            do {
                guard let url = Bundle.main.url(forResource: "backend", withExtension: "json") else {
                    throw NSError(domain: "APEE", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing backend"])
                }
                let backend = try JSONDecoder().decode(Backend.self, from: Data(contentsOf: url))
                let result = try backend.run(["snapshot"])
                print(String(decoding: result, as: UTF8.self))
                exit(0)
            } catch {
                fputs(error.localizedDescription + "\n", stderr)
                exit(1)
            }
        }
    }
    var body: some Scene {
        WindowGroup { Dashboard() }
    }
}
