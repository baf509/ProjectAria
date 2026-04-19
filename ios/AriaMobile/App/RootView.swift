import SwiftUI

struct RootView: View {
    @Environment(\.horizontalSizeClass) private var sizeClass
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        Group {
            if !settings.hasBaseURL {
                FirstRunView()
            } else if sizeClass == .regular {
                PadRootView()
            } else {
                PhoneRootView()
            }
        }
    }
}

// MARK: - Phone (compact)

struct PhoneRootView: View {
    @State private var selectedTab: Tab = .shells

    enum Tab: Hashable { case shells, chat, memory, settings }

    var body: some View {
        TabView(selection: $selectedTab) {
            NavigationStack { ShellsListView() }
                .tabItem { Label("Shells", systemImage: "terminal.fill") }
                .tag(Tab.shells)

            NavigationStack { ConversationsListView() }
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right.fill") }
                .tag(Tab.chat)

            NavigationStack { MemorySearchView() }
                .tabItem { Label("Memory", systemImage: "brain") }
                .tag(Tab.memory)

            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(Tab.settings)
        }
        .onAppear {
            let tabAppearance = UITabBarAppearance()
            tabAppearance.configureWithOpaqueBackground()
            tabAppearance.backgroundColor = UIColor(Neon.void)
            UITabBar.appearance().standardAppearance = tabAppearance
            UITabBar.appearance().scrollEdgeAppearance = tabAppearance

            let navAppearance = UINavigationBarAppearance()
            navAppearance.configureWithOpaqueBackground()
            navAppearance.backgroundColor = UIColor(Neon.void)
            navAppearance.titleTextAttributes = [
                .foregroundColor: UIColor(Neon.textPrimary),
                .font: UIFont.monospacedSystemFont(ofSize: 17, weight: .semibold)
            ]
            navAppearance.largeTitleTextAttributes = [
                .foregroundColor: UIColor(Neon.pink),
                .font: UIFont.monospacedSystemFont(ofSize: 34, weight: .bold)
            ]
            UINavigationBar.appearance().standardAppearance = navAppearance
            UINavigationBar.appearance().scrollEdgeAppearance = navAppearance
            UINavigationBar.appearance().compactAppearance = navAppearance
        }
    }
}

// MARK: - iPad (regular)

struct PadRootView: View {
    @State private var selection: PadSidebar.Item? = .shells

    var body: some View {
        NavigationSplitView {
            PadSidebar(selection: $selection)
        } detail: {
            NavigationStack {
                switch selection ?? .shells {
                case .shells:   ShellsListView()
                case .chat:     ConversationsListView()
                case .memory:   MemorySearchView()
                case .settings: SettingsView()
                }
            }
        }
    }
}

struct PadSidebar: View {
    enum Item: Hashable, CaseIterable, Identifiable {
        case shells, chat, memory, settings
        var id: Self { self }
        var label: String {
            switch self {
            case .shells: return "Shells"
            case .chat: return "Chat"
            case .memory: return "Memory"
            case .settings: return "Settings"
            }
        }
        var icon: String {
            switch self {
            case .shells: return "terminal.fill"
            case .chat: return "bubble.left.and.bubble.right.fill"
            case .memory: return "brain"
            case .settings: return "gearshape"
            }
        }
    }

    @Binding var selection: Item?

    var body: some View {
        List(Item.allCases, selection: $selection) { item in
            Label(item.label, systemImage: item.icon)
                .foregroundStyle(Neon.textPrimary)
        }
        .scrollContentBackground(.hidden)
        .background(Neon.void)
        .navigationTitle("Aria")
    }
}

// MARK: - First run

struct FirstRunView: View {
    @Environment(SettingsStore.self) private var settings
    @State private var draftURL: String = "http://"

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Base URL (e.g. http://aria.tailnet:8000)", text: $draftURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section {
                    Button("Continue") {
                        settings.setBaseURL(draftURL)
                    }
                    .disabled(URL(string: draftURL)?.scheme == nil)
                    .foregroundStyle(Neon.cyan)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Neon.void)
            .navigationTitle("Welcome to Aria")
        }
    }
}
