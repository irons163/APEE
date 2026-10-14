import SwiftUI
import AppKit
import UserNotifications

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
        let timeout = arguments.prefix(2).elementsEqual(["portfolio", "plan"]) ? 1800.0 : 90.0
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
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
    var projects: [[String: Any]] { data["projects"] as? [[String: Any]] ?? [] }
    var history: [[String: Any]] { data["budget_history"] as? [[String: Any]] ?? [] }
    var portfolio: [String: Any] { data["portfolio"] as? [String: Any] ?? [:] }
    func records(_ kind: String) -> [[String: Any]] { portfolio[kind] as? [[String: Any]] ?? [] }

    func jsonCommand(_ prefix: [String], flag: String, data: [String: Any], completed: (() -> Void)? = nil) {
        do {
            let payload = try JSONSerialization.data(withJSONObject: data, options: [.sortedKeys])
            command(prefix + [flag, String(decoding: payload, as: UTF8.self)], completed: completed)
        } catch { message = error.localizedDescription; self.error = true }
    }

    func portfolioCommand(_ action: String, data: [String: Any], completed: (() -> Void)? = nil) {
        jsonCommand(["portfolio", action], flag: "--data-json", data: data, completed: completed)
    }

    func command(_ args: [String], success: String = "操作已完成。", completed: (() -> Void)? = nil) {
        guard !busy else { return }
        message = ""
        error = false
        perform(args, snapshot: false, success: success, completed: completed)
    }

    func refresh() {
        guard !busy else { return }
        perform(["snapshot"], snapshot: true)
    }

    private func perform(_ args: [String], snapshot: Bool, success: String = "", completed: (() -> Void)? = nil) {
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
                let planResult = args.prefix(2).elementsEqual(["portfolio", "plan"]) ? (try? JSONSerialization.jsonObject(with: result)) as? [String: Any] : nil
                DispatchQueue.main.async {
                    self.busy = false
                    if snapshot {
                        if let object = object { self.data = object }
                        else { self.message = "無法辨識狀態資料。"; self.error = true }
                    } else {
                        if let blocked = planResult?["blocked"] as? [String: Any] { self.message = value(blocked, "message"); self.error = true }
                        else if let failure = planResult?["error"] as? String { self.message = failure; self.error = true }
                        else { self.message = success; self.error = false }
                        self.refresh()
                        completed?()
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

struct RecordRow: Identifiable {
    let id: String
    let data: [String: Any]
}

func identified(_ records: [[String: Any]], _ namespace: String, field: String = "id") -> [RecordRow] {
    records.map { RecordRow(id: namespace + ":" + value($0, field), data: $0) }
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
    @State private var taskOpen = false
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
                Text("專案設定").tag("專案設定")
                Text("用量與預算").tag("用量與預算")
                Text("恢復政策").tag("恢復政策")
                Text("目標與依賴").tag("目標與依賴")
                Text("回饋與成效").tag("回饋與成效")
                Text("審查與通知").tag("審查與通知")
            }.pickerStyle(.segmented)
            Group {
                if tab == "用量與預算" { BudgetPanel(model: model) }
                else if tab == "專案設定" { ProjectsPanel(model: model) }
                else if tab == "恢復政策" { RecoveryPanel(model: model) }
                else if tab == "目標與依賴" { GoalsPanel(model: model) }
                else if tab == "回饋與成效" { EvidencePanel(model: model) }
                else if tab == "審查與通知" { WorkflowPanel(model: model) }
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
            .sheet(isPresented: $taskOpen) { TaskPanel(model: model) }
    }

    var overview: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label(model.paused ? "派工已暫停，當前任務仍可完成" : "依佇列與專案優先順序推進", systemImage: "circle.fill")
                    .foregroundStyle(model.paused ? .orange : .teal)
                Spacer()
                Button("加入任務", systemImage: "plus") { taskOpen = true }.disabled(model.busy)
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
                    ForEach(identified(model.tasks, "task")) { row in
                        let task = row.data
                        HStack(alignment: .top, spacing: 18) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(value(task, "repo")).font(.caption).foregroundStyle(.secondary)
                                Text(value(task, "title")).font(.headline)
                                Text(value(task, "state_label")).foregroundStyle(.teal)
                                if let summary = task["pr_summary"] as? String { Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                                if let error = task["error"] as? String { Text(error).font(.caption).foregroundStyle(.secondary).lineLimit(3) }
                                if let review = task["code_review"] as? [String: Any] {
                                    Text("獨立審查：" + value(review, "summary") + " · 修復 " + value(task, "repairs") + " 次")
                                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                                }
                                if let ci = task["ci_repair_status"] as? [String: Any] { Text(value(ci, "message")).font(.caption).foregroundStyle(.orange).lineLimit(3) }
                                let decisions = (model.portfolio["scheduling"] as? [String: Any])?["tasks"] as? [[String: Any]] ?? []
                                if let decision = decisions.first(where: { $0["task"] as? String == task["id"] as? String }),
                                   let reasons = decision["reasons"] as? [String], !reasons.isEmpty {
                                    Text(reasons.joined(separator: "；")).font(.caption).foregroundStyle(.orange)
                                }
                            }
                            Spacer()
                            if let path = task["run_dir"] as? String {
                                Button("查看紀錄") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                            }
                            if let link = task["pr_url"] as? String, let url = URL(string: link), url.scheme == "https", url.host == "github.com" {
                                Button("查看 PR") { NSWorkspace.shared.open(url) }
                            }
                            if ["failed", "interrupted", "waiting"].contains(value(task, "state")) {
                                Button("重新入列") { model.command(["retry", value(task, "id")]) }.disabled(model.busy)
                            }
                            if ["preparing", "running", "testing"].contains(value(task, "state")) {
                                Button("核對恢復") { model.command(["recover", value(task, "id")]) }.disabled(model.busy)
                            }
                            if ["ready", "publishing"].contains(value(task, "state")) {
                                Button("交付／核對 PR") { model.command(["publish", value(task, "id")]) }.disabled(model.busy)
                            }
                            if ["queued", "waiting"].contains(value(task, "state")) {
                                Button("取消任務") { model.command(["cancel", value(task, "id")]) }.disabled(model.busy)
                            }
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.background, in: RoundedRectangle(cornerRadius: 12))
                    }
                }
            }
        }
    }
}

struct FormFeedback: View {
    @ObservedObject var model: DashboardModel
    var body: some View {
        if model.error && !model.message.isEmpty {
            Text(model.message).font(.callout).foregroundStyle(.red).textSelection(.enabled)
        }
    }
}

func commandLines(_ text: String) throws -> [String] {
    try text.components(separatedBy: .newlines).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }.enumerated().map { index, line in
        guard let data = line.data(using: .utf8),
              let command = try? JSONSerialization.jsonObject(with: data) as? [String],
              !command.isEmpty, command.allSatisfy({ !$0.isEmpty && !$0.contains("\0") }) else {
            throw NSError(domain: "APEE", code: 1, userInfo: [NSLocalizedDescriptionKey: "第 \(index + 1) 行必須是非空命令陣列，例如 [\"python3\",\"-m\",\"unittest\"]。"])
        }
        return String(decoding: try JSONSerialization.data(withJSONObject: command), as: UTF8.self)
    }
}

func projectCommands(_ project: [String: Any], _ key: String) -> String {
    guard let commands = project[key] as? [[String]], !commands.isEmpty else { return "未設定" }
    return commands.map { command in
        guard let data = try? JSONSerialization.data(withJSONObject: command, options: [.withoutEscapingSlashes]) else { return "—" }
        return String(decoding: data, as: UTF8.self)
    }.joined(separator: "\n")
}

struct ProjectsPanel: View {
    @ObservedObject var model: DashboardModel
    @State private var projectOpen = false
    @State private var selection: ProjectSelection?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("已設定的專案").font(.title2.bold())
                Spacer()
                Button("加入專案", systemImage: "plus") { projectOpen = true }.disabled(model.busy)
            }
            Text("只有啟用的專案會接收任務。準備與測試命令決定成果是否可以交付。")
                .foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if model.projects.isEmpty { Text("尚未設定專案，請先加入一個 GitHub repository。") }
                    ForEach(identified(model.projects, "project", field: "repo")) { row in
                        let project = row.data
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text(value(project, "repo")).font(.headline)
                                Spacer()
                                Text(project["enabled"] as? Bool == true ? "已啟用" : "未啟用").foregroundStyle(.teal)
                                Text("優先順序 " + value(project, "priority")).foregroundStyle(.secondary)
                                Button("編輯") { selection = ProjectSelection(project: project) }.disabled(model.busy)
                            }
                            Text("Issue 標籤：" + value(project, "issue_label") + " · " + (project["publish_draft"] as? Bool == true ? "驗證後交付 Draft PR" : "保留本機審查包"))
                                .font(.callout).foregroundStyle(.secondary)
                            Text("測試").font(.caption.bold())
                            Text(projectCommands(project, "tests")).font(.callout.monospaced()).textSelection(.enabled)
                            if !(project["setup"] as? [[String]] ?? []).isEmpty {
                                Text("準備步驟" + (project["setup_network"] as? Bool == true ? "（允許連網）" : "（不連網）")).font(.caption.bold())
                                Text(projectCommands(project, "setup")).font(.callout.monospaced()).textSelection(.enabled)
                            }
                            let paths = project["allowed_paths"] as? [String]
                            Text("修改範圍：" + (paths?.joined(separator: "、") ?? "repository 內檔案")).font(.caption).foregroundStyle(.secondary)
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.background, in: RoundedRectangle(cornerRadius: 12))
                    }
                }
            }
        }.sheet(isPresented: $projectOpen) { AddProjectPanel(model: model) }
            .sheet(item: $selection) { selected in EditProjectPanel(model: model, project: selected.project) }
    }
}

struct AddProjectPanel: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var repo = ""
    @State private var tests = ""
    @State private var setup = ""
    @State private var priority = 3
    @State private var network = false
    @State private var publish = false
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("加入專案").font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    TextField("GitHub repository，例如 owner/project", text: $repo).textFieldStyle(.roundedBorder)
                        .accessibilityLabel("GitHub repository")
                    Stepper("優先順序 \(priority)（1 最高）", value: $priority, in: 1...5)
                    Text("測試命令（必填，每行一個參數陣列）").font(.headline)
                    Text("例如 [\"python3\",\"-m\",\"unittest\"] 或 [\"npm\",\"test\"]；請使用這個專案真正的驗證命令。")
                        .font(.callout).foregroundStyle(.secondary)
                    TextEditor(text: $tests).font(.callout.monospaced()).frame(height: 90)
                        .accessibilityLabel("測試命令")
                    Text("準備命令（選填，同樣每行一個參數陣列）").font(.headline)
                    TextEditor(text: $setup).font(.callout.monospaced()).frame(height: 70)
                        .accessibilityLabel("準備命令")
                    Toggle("允許準備步驟連網（例如安裝依賴）", isOn: $network)
                    Toggle("驗證通過後推送分支並建立 Draft PR", isOn: $publish)
                    Text("啟用後會接收你加入的任務，以及帶 apee:ready 標籤的開放 Issues。命令使用參數陣列，不經 shell 展開；既有專案與命令模板會保留。")
                        .font(.callout).foregroundStyle(.secondary)
                    FormFeedback(model: model)
                }.padding(.trailing, 8)
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.disabled(model.busy)
                Button("儲存並啟用專案") { save() }.buttonStyle(.borderedProminent).tint(.teal)
                    .disabled(model.busy || repo.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || tests.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(26).frame(width: 700, height: 640)
            .onAppear { model.message = ""; model.error = false }
    }

    func save() {
        do {
            let testCommands = try commandLines(tests)
            let setupCommands = try commandLines(setup)
            guard !testCommands.isEmpty else { return }
            var args = ["project-add", repo.trimmingCharacters(in: .whitespacesAndNewlines), "--priority", String(priority)]
            for command in testCommands { args += ["--test", command] }
            for command in setupCommands { args += ["--setup", command] }
            if network { args.append("--setup-network") }
            if publish { args.append("--publish-draft") }
            model.command(args, success: "專案已儲存並啟用。") { dismiss() }
        } catch {
            model.message = error.localizedDescription
            model.error = true
        }
    }
}

struct TaskPanel: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var repo = ""
    @State private var taskTitle = ""
    @State private var details = ""
    var enabled: [[String: Any]] { model.projects.filter { $0["enabled"] as? Bool == true } }
    var selected: [String: Any] { enabled.first { $0["repo"] as? String == repo } ?? [:] }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("加入開發任務").font(.title2.bold())
            if enabled.isEmpty {
                Text("請先在「專案設定」加入並啟用專案。").foregroundStyle(.secondary)
            } else {
                Picker("專案", selection: $repo) {
                    ForEach(Array(enabled.enumerated()), id: \.offset) { _, project in
                        Text(value(project, "repo")).tag(value(project, "repo"))
                    }
                }
                TextField("任務標題", text: $taskTitle).textFieldStyle(.roundedBorder).accessibilityLabel("任務標題")
                Text("需求與完成條件").font(.headline)
                TextEditor(text: $details).frame(minHeight: 160).accessibilityLabel("需求與完成條件")
                Text(selected["publish_draft"] as? Bool == true ? "驗證通過後依專案設定交付 Draft PR。" : "驗證通過後保留本機審查包。")
                    .font(.callout).foregroundStyle(.secondary)
                Text("加入佇列後，背景排程會依優先順序、暫停與預算處理。相同標題與內容不會重複入列。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            FormFeedback(model: model)
            HStack {
                Spacer()
                Button("取消") { dismiss() }.disabled(model.busy)
                Button("加入佇列") {
                    model.command(["enqueue", repo, "--title=" + taskTitle, "--body=" + details],
                                  success: "已核對任務入列；相同內容不會重複加入。") { dismiss() }
                }.buttonStyle(.borderedProminent).tint(.teal)
                    .disabled(model.busy || selected.isEmpty || taskTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || details.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(26).frame(width: 680, height: enabled.isEmpty ? 220 : 520)
            .onAppear {
                repo = enabled.first?["repo"] as? String ?? ""
                model.message = ""; model.error = false
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
    @State private var day = ""
    var usage: [String: Any] { model.history.first { $0["date"] as? String == day } ?? model.budget }
    var historical: Bool { value(usage, "date") != value(model.budget, "date") }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("預算控制新的模型派工").font(.title2.bold())
                Text(value(model.budget, "notice")).foregroundStyle(.secondary)
                Picker("查看用量日期（UTC）", selection: $day) {
                    Text("今天 · " + value(model.budget, "date")).tag("")
                    ForEach(Array(model.history.filter { value($0, "date") != value(model.budget, "date") }.enumerated()), id: \.offset) { _, summary in
                        Text(value(summary, "date")).tag(value(summary, "date"))
                    }
                }.frame(maxWidth: 400)
                HStack(spacing: 24) {
                    Text("輸入 " + value(usage, "input_tokens"))
                    Text("其中快取 " + value(usage, "cached_input_tokens"))
                    Text("輸出 " + value(usage, "output_tokens"))
                    Text("估算 USD " + value(usage, "estimated_usd", fallback: "未定價"))
                }.font(.callout.monospacedDigit())
                let unknown = usage["unknown_runs"] as? [String] ?? []
                let unpriced = usage["unpriced_runs"] as? [String] ?? []
                if !unknown.isEmpty || !unpriced.isEmpty {
                    Text("用量待核對 \(unknown.count) 回合；費用未定價 \(unpriced.count) 回合。" + (historical ? "" : "啟用相關預算後，這些回合會阻擋新派工。"))
                        .foregroundStyle(.orange)
                }
                if historical { Text("正在查看歷史用量。下方設定套用於目前的派工預算，歷史回合不計入今天。")
                    .font(.callout).foregroundStyle(.secondary) }
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
                Text("模型回合 · " + value(usage, "date")).font(.headline)
                let records = usage["records"] as? [[String: Any]] ?? []
                ForEach(Array(records.enumerated()), id: \.offset) { _, record in
                    HStack {
                        Text(String(value(record, "run_id").prefix(12))).font(.callout.monospaced())
                        Text(value(record, "phase", fallback: "舊版回合")).font(.caption).foregroundStyle(.secondary)
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
                        let relative = value(record, "log_relative", fallback: "runs/" + value(record, "attempt_id", fallback: value(record, "run_id")) + "/agent-events.log")
                        let base = URL(fileURLWithPath: root).standardizedFileURL
                        let url = base.appendingPathComponent(relative).standardizedFileURL.resolvingSymlinksInPath()
                        if url.path.hasPrefix(base.path + "/") { NSWorkspace.shared.open(url) }
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

struct ProjectSelection: Identifiable {
    let project: [String: Any]
    var id: String { value(project, "repo") }
}

struct EditProjectPanel: View {
    @ObservedObject var model: DashboardModel
    let project: [String: Any]
    @Environment(\.dismiss) private var dismiss
    @State private var enabled = false
    @State private var priority = 3
    @State private var label = ""
    @State private var baseBranch = ""
    @State private var tests = ""
    @State private var setup = ""
    @State private var paths = ""
    @State private var network = false
    @State private var publish = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("編輯專案 · " + value(project, "repo")).font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Toggle("啟用這個專案", isOn: $enabled)
                    Stepper("優先順序 \(priority)（1 最高）", value: $priority, in: 1...5)
                    TextField("Issue 派工標籤", text: $label).textFieldStyle(.roundedBorder)
                    TextField("交付目標分支（留空使用 repository 預設）", text: $baseBranch).textFieldStyle(.roundedBorder)
                    Text("測試命令（每行一個參數陣列）").font(.headline)
                    TextEditor(text: $tests).font(.callout.monospaced()).frame(height: 90).accessibilityLabel("既有專案測試命令")
                    Text("準備命令").font(.headline)
                    TextEditor(text: $setup).font(.callout.monospaced()).frame(height: 65)
                    Toggle("允許準備步驟連網", isOn: $network)
                    Toggle("驗證後交付 Draft PR", isOn: $publish)
                    Text("允許修改的檔案（每行一個相對路徑；留空表示整個 repository）")
                    TextEditor(text: $paths).font(.callout.monospaced()).frame(height: 65)
                    FormFeedback(model: model)
                }.padding(.trailing, 8)
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.disabled(model.busy)
                Button("儲存專案修改") { save() }.buttonStyle(.borderedProminent).disabled(model.busy)
            }
        }.padding(26).frame(width: 700, height: 690).onAppear { load() }
    }
    func load() {
        enabled = project["enabled"] as? Bool ?? false
        priority = project["priority"] as? Int ?? 3
        label = value(project, "issue_label", fallback: "apee:ready")
        baseBranch = value(project, "base_branch", fallback: "")
        tests = (project["tests"] as? [[String]] ?? []).isEmpty ? "" : projectCommands(project, "tests")
        setup = (project["setup"] as? [[String]] ?? []).isEmpty ? "" : projectCommands(project, "setup")
        paths = (project["allowed_paths"] as? [String] ?? []).joined(separator: "\n")
        network = project["setup_network"] as? Bool ?? false
        publish = project["publish_draft"] as? Bool ?? false
        model.message = ""; model.error = false
    }
    func save() {
        do {
            let testData = try commandLines(tests).map { try JSONSerialization.jsonObject(with: Data($0.utf8)) }
            let setupData = try commandLines(setup).map { try JSONSerialization.jsonObject(with: Data($0.utf8)) }
            let allowed = paths.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            let changes: [String: Any] = ["enabled": enabled, "priority": priority, "issue_label": label,
                "tests": testData, "setup": setupData, "setup_network": network, "publish_draft": publish,
                "allowed_paths": allowed.isEmpty ? NSNull() : allowed as Any]
            model.jsonCommand(["project-edit", value(project, "repo"), "--expected-revision", value(project, "revision")],
                flag: "--changes-json", data: changes.merging(["base_branch": baseBranch.isEmpty ? NSNull() : baseBranch as Any]) { _, new in new }) { dismiss() }
        } catch { model.message = error.localizedDescription; model.error = true }
    }
}

struct PortfolioSelection: Identifiable {
    let action: String
    var record: [String: Any] = [:]
    var id: String { action + value(record, "id", fallback: "new") }
}

struct GoalsPanel: View {
    @ObservedObject var model: DashboardModel
    @State private var selection: PortfolioSelection?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("跨專案目標與依賴").font(.title2.bold())
                Spacer()
                Button("加入目標") { selection = PortfolioSelection(action: "goals") }
                Button("連結任務與依賴") { selection = PortfolioSelection(action: "link") }
            }.disabled(model.busy)
            let scheduling = model.portfolio["scheduling"] as? [String: Any] ?? [:]
            Text("同時推進專案：" + (scheduling["active_projects"] as? [String] ?? []).joined(separator: "、") + " · 上限 " + value(scheduling, "limit"))
                .foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if model.records("goals").isEmpty { Text("先設定目標與完成條件，再連結任務或收集回饋。").foregroundStyle(.secondary) }
                    ForEach(identified(model.records("goals"), "goal")) { row in
                        let goal = row.data
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text(value(goal, "title")).font(.headline)
                                Spacer()
                                Text(goal["enabled"] as? Bool == true ? "已啟用" : "已停用").foregroundStyle(.secondary)
                                Button("編輯目標") { selection = PortfolioSelection(action: "goals", record: goal) }
                            }
                            Text(value(goal, "objective"))
                            Text("完成條件：" + value(goal, "criteria")).font(.callout).foregroundStyle(.secondary)
                            Text((goal["repos"] as? [String] ?? []).joined(separator: " · ")).font(.caption)
                            Text(value(goal, "autonomy") == "enqueue" ? "允許依政策自動加入候選工作" : "候選工作由你決定是否入列").font(.caption).foregroundStyle(.teal)
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).background(.background, in: RoundedRectangle(cornerRadius: 12))
                    }
                    Divider()
                    Text("任務與依賴").font(.headline)
                    ForEach(identified(model.tasks, "linked-task")) { row in
                        let task = row.data
                        let link = model.records("links").first { $0["id"] as? String == task["id"] as? String } ?? [:]
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(value(task, "title"))
                                let goal = model.records("goals").first { $0["id"] as? String == link["goal"] as? String } ?? [:]
                                Text(value(goal, "title", fallback: "尚未連結目標") + " · " + value(task, "state_label")).font(.caption).foregroundStyle(.secondary)
                                let deps = link["depends_on"] as? [String] ?? []
                                if !deps.isEmpty { Text("等待／參考：" + deps.map { id in value(model.tasks.first { $0["id"] as? String == id } ?? [:], "title") }.joined(separator: "、")).font(.caption) }
                            }
                            Spacer()
                            if value(task, "state") == "queued" {
                                Button("編輯連結") { selection = PortfolioSelection(action: "link", record: task) }
                            }
                            if value(task, "state") == "ready" {
                                Button("確認本機成果完成") { selection = PortfolioSelection(action: "complete-local", record: task) }
                            }
                        }.padding(10)
                    }
                }
            }
        }.sheet(item: $selection) { item in PortfolioForm(model: model, selection: item) }
    }
}

struct EvidencePanel: View {
    @ObservedObject var model: DashboardModel
    @State private var selection: PortfolioSelection?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("回饋、指標與成效").font(.title2.bold())
                Spacer()
                Button("加入回饋") { selection = PortfolioSelection(action: "feedback") }
                Button("定義指標") { selection = PortfolioSelection(action: "definitions") }
                Button("記錄指標") { selection = PortfolioSelection(action: "metrics") }
            }.disabled(model.busy)
            HStack {
                Button("依目前資料規劃") { model.command(["portfolio", "plan"]) }
                Button("核對成效") { model.command(["portfolio", "measure"]) }
                Text("合併與本機驗證完成後，仍需交付後的觀測資料。").font(.caption).foregroundStyle(.secondary)
            }.disabled(model.busy)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    Text("候選工作").font(.headline)
                    if model.records("candidates").isEmpty { Text("尚無候選工作；啟用規劃政策後，會依目標及證據提出有限數量的建議。").foregroundStyle(.secondary) }
                    ForEach(identified(model.records("candidates"), "candidate")) { row in
                        let item = row.data
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text(value(item, "title")).font(.headline)
                                Spacer()
                                Text(["pending": "待決定", "accepted": "已入列", "rejected": "已拒絕"][value(item, "state")] ?? value(item, "state"))
                                if value(item, "state") == "pending" {
                                    Button("接受") { selection = PortfolioSelection(action: "accept", record: item) }
                                    Button("拒絕") { selection = PortfolioSelection(action: "reject", record: item) }
                                }
                            }
                            Text(value(item, "reason"))
                            Text(value(item, "body")).font(.callout)
                            Text("驗收：" + value(item, "acceptance")).font(.callout).foregroundStyle(.secondary)
                            Text("證據：" + (item["sources"] as? [String] ?? []).joined(separator: "、")).font(.caption).textSelection(.enabled)
                            let outcome = model.records("outcomes").first { $0["candidate"] as? String == item["id"] as? String } ?? [:]
                            if !outcome.isEmpty {
                                Text("成效：" + outcomeLabel(value(outcome, "status")) + " · " + value(outcome, "reason")).font(.callout).foregroundStyle(.teal)
                                ForEach(Array((outcome["comparisons"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, comparison in
                                    let definition = model.records("definitions").first { $0["id"] as? String == comparison["definition"] as? String } ?? [:]
                                    Text(value(definition, "name") + "：" + value(comparison, "before_value") + " → " + value(comparison, "after_value") + " " + value(definition, "unit")).font(.caption)
                                }
                            }
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).background(.background, in: RoundedRectangle(cornerRadius: 12))
                    }
                    Text("使用者回饋").font(.headline)
                    ForEach(identified(model.records("feedback"), "feedback")) { row in
                        let item = row.data
                        VStack(alignment: .leading, spacing: 4) {
                            Text(value(item, "text"))
                            Text(value(item, "repo") + " · " + value(item, "source") + " · " + value(item, "observed_at")).font(.caption).foregroundStyle(.secondary)
                            Text(value(item, "id")).font(.caption.monospaced()).textSelection(.enabled)
                        }.padding(10)
                    }
                    Text("產品指標").font(.headline)
                    ForEach(identified(model.records("definitions"), "definition")) { row in
                        let definition = row.data
                        let samples = model.records("metrics").filter { $0["definition"] as? String == definition["id"] as? String }.sorted { value($0, "observed_at") > value($1, "observed_at") }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(value(definition, "name") + " · 目標 " + (value(definition, "direction") == "lte" ? "≤ " : "≥ ") + value(definition, "target") + " " + value(definition, "unit"))
                            ForEach(Array(samples.prefix(3).enumerated()), id: \.offset) { _, sample in
                                Text(value(sample, "value") + " · " + value(sample, "source") + " · " + value(sample, "observed_at")).font(.caption).foregroundStyle(.secondary)
                            }
                        }.padding(10)
                    }
                    Text("規劃執行紀錄").font(.headline)
                    ForEach(identified(model.records("jobs"), "job")) { row in
                        let job = row.data
                        HStack {
                            Text(value(job, "started_at") + " · " + value(job, "state")).font(.caption)
                            if let error = job["error"] as? String { Text(error).font(.caption).foregroundStyle(.orange) }
                            Spacer()
                            if let path = job["run_dir"] as? String { Button("查看規劃記錄") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) } }
                        }
                    }
                }
            }
        }.sheet(item: $selection) { item in PortfolioForm(model: model, selection: item) }
    }
    func outcomeLabel(_ state: String) -> String {
        ["unknown": "等待觀測", "confirmed": "當時指標達標", "not_met": "尚未達標", "regressed": "指標退步"][state] ?? state
    }
}

struct PortfolioForm: View {
    @ObservedObject var model: DashboardModel
    let selection: PortfolioSelection
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var objective = ""
    @State private var criteria = ""
    @State private var source = ""
    @State private var goal = ""
    @State private var repo = ""
    @State private var definition = ""
    @State private var task = ""
    @State private var unit = ""
    @State private var target = ""
    @State private var direction = "lte"
    @State private var observed = Date()
    @State private var repos = Set<String>()
    @State private var dependencies = Set<String>()
    @State private var priority = 3
    @State private var enabled = true
    @State private var autonomy = "propose"
    var heading: String { ["goals": "設定目標", "feedback": "加入使用者回饋", "definitions": "定義產品指標", "metrics": "記錄指標觀測", "link": "任務目標與依賴", "complete-local": "確認本機成果", "accept": "接受候選工作", "reject": "拒絕候選工作"][selection.action] ?? "設定" }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(heading).font(.title2.bold())
            ScrollView { fields.padding(.trailing, 8) }
            FormFeedback(model: model)
            HStack {
                Spacer()
                Button("取消") { dismiss() }.disabled(model.busy)
                Button("儲存") { save() }.buttonStyle(.borderedProminent).disabled(model.busy)
            }
        }.padding(26).frame(width: 700, height: 640).onAppear { load() }
    }
    @ViewBuilder var fields: some View {
        VStack(alignment: .leading, spacing: 14) {
            if selection.action == "goals" {
                TextField("目標名稱", text: $title).textFieldStyle(.roundedBorder).accessibilityLabel("目標名稱")
                Text("希望達到的結果")
                TextEditor(text: $objective).frame(height: 70).accessibilityLabel("目標內容")
                Text("如何判斷完成")
                TextEditor(text: $criteria).frame(height: 70).accessibilityLabel("目標完成條件")
                Text("關聯專案").font(.headline)
                ForEach(Array(model.projects.enumerated()), id: \.offset) { _, project in
                    Toggle(value(project, "repo"), isOn: Binding(get: { repos.contains(value(project, "repo")) }, set: { on in
                        if on { repos.insert(value(project, "repo")) } else { repos.remove(value(project, "repo")) }
                    }))
                }
                Stepper("目標優先順序 \(priority)", value: $priority, in: 1...5)
                Toggle("啟用這個目標", isOn: $enabled)
                Picker("候選工作", selection: $autonomy) {
                    Text("先提出候選，由我決定").tag("propose")
                    Text("依有限政策自動入列").tag("enqueue")
                }
            } else if ["feedback", "definitions", "link"].contains(selection.action) {
                if selection.action == "link" {
                    Picker("任務", selection: $task) {
                        ForEach(Array(model.tasks.filter { value($0, "state") == "queued" }.enumerated()), id: \.offset) { _, item in Text(value(item, "title")).tag(value(item, "id")) }
                    }.onChange(of: task) { _ in loadLink() }
                }
                Picker("目標", selection: $goal) {
                    if selection.action == "link" { Text("不指定目標").tag("") }
                    ForEach(Array(model.records("goals").enumerated()), id: \.offset) { _, item in Text(value(item, "title")).tag(value(item, "id")) }
                }
                if selection.action == "link" {
                    Text("依賴其他任務通過審查／合併，或經你確認本機成果完成後才開始。").font(.callout).foregroundStyle(.secondary)
                    ForEach(Array(model.tasks.filter { value($0, "id") != task }.enumerated()), id: \.offset) { _, item in
                        Toggle(value(item, "title") + " · " + value(item, "state_label"), isOn: Binding(get: { dependencies.contains(value(item, "id")) }, set: { on in
                            if on { dependencies.insert(value(item, "id")) } else { dependencies.remove(value(item, "id")) }
                        }))
                    }
                } else {
                    Picker("專案", selection: $repo) {
                        ForEach(Array(model.projects.enumerated()), id: \.offset) { _, project in Text(value(project, "repo")).tag(value(project, "repo")) }
                    }
                    if selection.action == "feedback" {
                        Text("實際收到的回饋")
                        TextEditor(text: $objective).frame(height: 150).accessibilityLabel("回饋內容")
                        evidenceFields
                    } else {
                        TextField("指標名稱", text: $title).textFieldStyle(.roundedBorder)
                        TextField("單位，例如秒、百分比、次／週", text: $unit).textFieldStyle(.roundedBorder)
                        Picker("達標方向", selection: $direction) { Text("不超過目標").tag("lte"); Text("至少達到目標").tag("gte") }
                        TextField("目標數值", text: $target).textFieldStyle(.roundedBorder)
                        Text("定義保存後不能改寫；若測量方式改變，請新增定義。").font(.callout).foregroundStyle(.secondary)
                    }
                }
            } else if selection.action == "metrics" {
                Picker("指標", selection: $definition) {
                    ForEach(Array(model.records("definitions").enumerated()), id: \.offset) { _, item in Text(value(item, "name") + " · " + value(item, "unit")).tag(value(item, "id")) }
                }
                TextField("這次觀測數值", text: $target).textFieldStyle(.roundedBorder).accessibilityLabel("指標觀測數值")
                evidenceFields
                Text("填入真實來源與觀測時間。原始資料會保留；成效只以交付後、相同定義的指標比較。").font(.callout).foregroundStyle(.secondary)
            } else {
                Text(value(selection.record, "title")).font(.headline)
                Text(selection.action == "complete-local" ? "這會讓依賴這項本機成果的工作可以開始。請先檢查審查包與實際驗收。" : "記錄這次決策的原因。接受後，候選工作會加入正常佇列。")
                TextEditor(text: $source).frame(height: 140).accessibilityLabel("決策原因")
            }
        }
    }
    var evidenceFields: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("來源與依據", text: $source).textFieldStyle(.roundedBorder).accessibilityLabel("觀測來源")
            DatePicker("觀測時間", selection: $observed)
        }
    }
    func load() {
        goal = model.records("goals").first?["id"] as? String ?? ""
        repo = model.projects.first?["repo"] as? String ?? ""
        definition = model.records("definitions").first?["id"] as? String ?? ""
        task = selection.record["id"] as? String ?? model.tasks.first { value($0, "state") == "queued" }?["id"] as? String ?? ""
        if selection.action == "goals", !selection.record.isEmpty {
            let item = selection.record
            title = value(item, "title", fallback: ""); objective = value(item, "objective", fallback: "")
            criteria = value(item, "criteria", fallback: ""); priority = item["priority"] as? Int ?? 3
            enabled = item["enabled"] as? Bool ?? true; autonomy = value(item, "autonomy", fallback: "propose")
            repos = Set(item["repos"] as? [String] ?? [])
        }
        if selection.action == "link" { loadLink() }
        model.message = ""; model.error = false
    }
    func loadLink() {
        let item = model.records("links").first { $0["id"] as? String == task } ?? [:]
        goal = value(item, "goal", fallback: "")
        dependencies = Set(item["depends_on"] as? [String] ?? [])
    }
    func save() {
        let date = ISO8601DateFormatter().string(from: observed)
        var action = selection.action
        var data: [String: Any]
        switch action {
        case "goals":
            data = ["title": title, "objective": objective, "criteria": criteria, "repos": repos.sorted(), "priority": priority, "enabled": enabled, "autonomy": autonomy]
            if !selection.record.isEmpty { data["id"] = selection.record["id"]; data["expected_updated"] = selection.record["updated"] }
        case "feedback": data = ["goal": goal, "repo": repo, "text": objective, "source": source, "observed_at": date]
        case "definitions": data = ["goal": goal, "repo": repo, "name": title, "unit": unit, "direction": direction, "target": target]
        case "metrics": data = ["definition": definition, "value": target, "source": source, "observed_at": date]
        case "link": data = ["task": task, "goal": goal.isEmpty ? NSNull() : goal as Any, "depends_on": dependencies.sorted()]
        case "complete-local": data = ["task": task, "reason": source]
        default:
            data = ["candidate": value(selection.record, "id"), "decision": action, "reason": source]
            action = "decide"
        }
        model.portfolioCommand(action, data: data) { dismiss() }
    }
}

struct WorkflowPanel: View {
    @ObservedObject var model: DashboardModel
    @State private var quality = false
    @State private var repairs = 1
    @State private var ci = false
    @State private var ciAttempts = 1
    @State private var planning = false
    @State private var projects = 2
    @State private var newTasks = 1
    @State private var pending = 5
    @State private var calls = 1
    @State private var interval = 300
    @State private var serviceTasks = 1
    @State private var permission = "檢查中"
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("審查、修復與主動規劃").font(.title2.bold())
                Toggle("啟用獨立程式碼審查與有限修復", isOn: $quality)
                Stepper("每次開發最多修復 \(repairs) 次", value: $repairs, in: 0...3)
                Toggle("自動修復 APEE PR 的 GitHub Actions 失敗", isOn: $ci)
                Stepper("每個 PR 累計最多建立 \(ciAttempts) 項 CI 修復任務", value: $ciAttempts, in: 1...3)
                Toggle("依目標與觀測資料提出改進候選", isOn: $planning)
                Stepper("同時推進最多 \(projects) 個專案", value: $projects, in: 1...100)
                Stepper("每輪最多提出 \(newTasks) 項工作", value: $newTasks, in: 1...5)
                Stepper("最多保留 \(pending) 項待決候選", value: $pending, in: 1...20)
                Stepper("每日最多 \(calls) 次規劃模型回合", value: $calls, in: 1...20)
                Text("所有模型回合共用用量預算。CI 修復一律重新測試與獨立審查；遠端版本改變時停止更新。候選是否自動入列，由每個目標設定。").font(.callout).foregroundStyle(.secondary)
                Button("儲存工作流程政策") {
                    model.save(["quality": ["enabled": quality, "max_repairs": repairs], "ci_repair": ["enabled": ci, "max_attempts_per_pr": ciAttempts],
                        "planning": ["enabled": planning, "max_active_projects": projects, "max_new_tasks_per_cycle": newTasks, "max_pending_candidates": pending, "max_calls_per_day": calls]])
                }.buttonStyle(.borderedProminent).disabled(model.busy)
                Divider()
                notificationPanel
                Divider()
                servicePanel
            }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
                .background(.background, in: RoundedRectangle(cornerRadius: 14))
        }.onAppear { load(); checkPermission() }
    }
    var notificationPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            let notification = model.data["notifications"] as? [String: Any] ?? [:]
            let policy = notification["policy"] as? [String: Any] ?? [:]
            Text("macOS 成果通知").font(.headline)
            Text((policy["enabled"] as? Bool == true ? "已啟用" : "未啟用") + " · 系統權限：" + permission + " · 待發 " + value(notification, "pending") + " · 失敗 " + value(notification, "failed"))
            HStack {
                Button("允許並啟用桌面通知") {
                    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
                        DispatchQueue.main.async {
                            checkPermission()
                            if granted { model.save(["notifications": ["enabled": true, "app_path": Bundle.main.bundlePath, "max_attempts": 6]]) }
                            else { model.message = error?.localizedDescription ?? "系統尚未允許通知；請到系統設定開啟 APEE 通知。"; model.error = true }
                        }
                    }
                }
                Button("停止桌面通知") { model.save(["notifications": ["enabled": false, "app_path": Bundle.main.bundlePath, "max_attempts": 6]]) }
                Button("開啟通知設定") { if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") { NSWorkspace.shared.open(url) } }
            }.disabled(model.busy)
            if let error = notification["adapter_error"] as? [String: Any] { Text(value(error, "message")).foregroundStyle(.orange).font(.caption) }
            ForEach(Array((notification["recent"] as? [[String: Any]] ?? []).prefix(8).enumerated()), id: \.offset) { _, item in
                HStack {
                    Text(value(item, "title") + " · " + value(item, "status")).font(.caption)
                    if let error = item["error"] as? String { Text(error).font(.caption).foregroundStyle(.orange) }
                    Spacer()
                    if ["failed", "blocked"].contains(value(item, "status")) { Button("重試通知") { model.command(["notifications", "retry", value(item, "id")]) } }
                }
            }
            Text("僅通知有意義的成果或需要處理的變化。系統接受後保存紀錄；實際横幅仍依 macOS 通知與專注模式設定。").font(.caption).foregroundStyle(.secondary)
        }
    }
    var servicePanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("背景服務").font(.headline)
            Stepper("每 \(interval) 秒檢查一輪", value: $interval, in: 60...86400, step: 60)
            Stepper("每次喚醒最多開始 \(serviceTasks) 次開發嘗試", value: $serviceTasks, in: 1...20)
            HStack {
                Button(model.service["installed"] as? Bool == true ? "更新服務設定" : "安裝背景服務") {
                    model.command(["service", "install", "--interval", String(interval), "--max-tasks", String(serviceTasks)])
                }
                Button("啟動") { model.command(["service", "start"]) }
                Button("停止") { model.command(["service", "stop"]) }
                Button("移除服務") { model.command(["service", "uninstall"]) }
            }.disabled(model.busy)
            Text("移除服務會保留設定、任務、用量與成果。安裝時會核對背景程序能否讀取所需檔案。").font(.caption).foregroundStyle(.secondary)
        }
    }
    func checkPermission() {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            DispatchQueue.main.async {
                permission = [UNAuthorizationStatus.notDetermined: "尚未決定", .denied: "已拒絕", .authorized: "已允許", .provisional: "暫時允許"][settings.authorizationStatus] ?? "未知"
            }
        }
    }
    func load() {
        let q = model.data["quality"] as? [String: Any] ?? [:]
        quality = q["enabled"] as? Bool ?? false; repairs = q["max_repairs"] as? Int ?? 1
        let c = model.data["ci_repair"] as? [String: Any] ?? [:]
        ci = c["enabled"] as? Bool ?? false; ciAttempts = c["max_attempts_per_pr"] as? Int ?? 1
        let p = model.data["planning"] as? [String: Any] ?? [:]
        planning = p["enabled"] as? Bool ?? false; projects = p["max_active_projects"] as? Int ?? 2
        newTasks = p["max_new_tasks_per_cycle"] as? Int ?? 1; pending = p["max_pending_candidates"] as? Int ?? 5; calls = p["max_calls_per_day"] as? Int ?? 1
    }
}

func waitForCallback<T>(_ operation: (@escaping (T) -> Void) -> Void) throws -> T {
    let semaphore = DispatchSemaphore(value: 0)
    var values: [T] = []
    operation { item in values.append(item); semaphore.signal() }
    let deadline = Date().addingTimeInterval(12)
    while semaphore.wait(timeout: .now()) != .success {
        if Date() >= deadline { throw NSError(domain: "APEE", code: 1, userInfo: [NSLocalizedDescriptionKey: "通知系統回應逾時"]) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
    return values[0]
}

final class NotificationAgent: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationAgent()
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
    func deliver(_ backend: Backend) throws {
        let center = UNUserNotificationCenter.current()
        let settings: UNNotificationSettings = try waitForCallback { center.getNotificationSettings(completionHandler: $0) }
        guard let pending = try JSONSerialization.jsonObject(with: backend.run(["notifications", "pending"])) as? [[String: Any]] else { return }
        let delivered: [UNNotification] = try waitForCallback { center.getDeliveredNotifications(completionHandler: $0) }
        let scheduled: [UNNotificationRequest] = try waitForCallback { center.getPendingNotificationRequests(completionHandler: $0) }
        let known = Set(delivered.map { $0.request.identifier } + scheduled.map { $0.identifier })
        for item in pending {
            let id = APEE.value(item, "id")
            guard let claim = try JSONSerialization.jsonObject(with: backend.run(["notifications", "claim", id])) as? [String: Any], claim["claimed"] as? Bool == true else { continue }
            let token = APEE.value(claim, "token")
            do {
                if !known.contains(id) {
                    guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                        throw NSError(domain: "APEE", code: 1, userInfo: [NSLocalizedDescriptionKey: "macOS 尚未允許 APEE 通知"])
                    }
                    let content = UNMutableNotificationContent()
                    content.title = APEE.value(item, "title")
                    content.body = APEE.value(item, "body")
                    content.sound = .default
                    let request = UNNotificationRequest(identifier: id, content: content, trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
                    let error: Error? = try waitForCallback { center.add(request, withCompletionHandler: $0) }
                    if let error = error { throw error }
                }
                _ = try backend.run(["notifications", "receipt", id, "--token", token])
            } catch {
                _ = try backend.run(["notifications", "receipt", id, "--token", token, "--error", error.localizedDescription])
            }
        }
    }
    func status() throws -> [String: Any] {
        let center = UNUserNotificationCenter.current()
        let settings: UNNotificationSettings = try waitForCallback { center.getNotificationSettings(completionHandler: $0) }
        let delivered: [UNNotification] = try waitForCallback { center.getDeliveredNotifications(completionHandler: $0) }
        let scheduled: [UNNotificationRequest] = try waitForCallback { center.getPendingNotificationRequests(completionHandler: $0) }
        return ["authorization": settings.authorizationStatus.rawValue, "delivered_ids": delivered.map { $0.request.identifier }, "pending_ids": scheduled.map { $0.identifier }]
    }
}

@main
struct APEEApp: App {
    init() {
        UNUserNotificationCenter.current().delegate = NotificationAgent.shared
        if CommandLine.arguments.contains("--diagnose") || CommandLine.arguments.contains("--deliver-notifications") || CommandLine.arguments.contains("--notification-status") {
            do {
                guard let url = Bundle.main.url(forResource: "backend", withExtension: "json") else {
                    throw NSError(domain: "APEE", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing backend"])
                }
                let backend = try JSONDecoder().decode(Backend.self, from: Data(contentsOf: url))
                if CommandLine.arguments.contains("--deliver-notifications") {
                    try NotificationAgent.shared.deliver(backend)
                } else if CommandLine.arguments.contains("--notification-status") {
                    print(String(decoding: try JSONSerialization.data(withJSONObject: NotificationAgent.shared.status()), as: UTF8.self))
                } else {
                    let result = try backend.run(["snapshot"])
                    print(String(decoding: result, as: UTF8.self))
                }
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
