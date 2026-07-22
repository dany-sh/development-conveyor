import ApplicationServices
import AppKit
import Foundation

enum SmokeFailure: Error, CustomStringConvertible {
    case message(String)

    var description: String {
        switch self {
        case .message(let value): value
        }
    }
}

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
        return nil
    }
    return value
}

func stringAttribute(_ element: AXUIElement, _ name: String) -> String? {
    attribute(element, name) as? String
}

func boolAttribute(_ element: AXUIElement, _ name: String) -> Bool? {
    attribute(element, name) as? Bool
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func descendants(_ root: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    guard depth < 24 else { return [] }
    return children(root).flatMap { child in [child] + descendants(child, depth: depth + 1) }
}

func accessibleStrings(_ element: AXUIElement) -> Set<String> {
    var values = Set<String>()
    for candidate in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
        if let value = stringAttribute(element, candidate), !value.isEmpty {
            values.insert(value)
        }
    }
    for child in descendants(element, depth: 20) {
        for candidate in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
            if let value = stringAttribute(child, candidate), !value.isEmpty {
                values.insert(value)
            }
        }
    }
    return values
}

func performPress(_ element: AXUIElement) -> Bool {
    if AXUIElementPerformAction(element, kAXPressAction as CFString) == .success {
        return true
    }
    for child in descendants(element) {
        var names: CFArray?
        if AXUIElementCopyActionNames(child, &names) == .success,
           let actions = names as? [String],
           actions.contains(kAXPressAction),
           AXUIElementPerformAction(child, kAXPressAction as CFString) == .success {
            return true
        }
    }
    var selectedSettable = DarwinBoolean(false)
    if AXUIElementIsAttributeSettable(
        element, kAXSelectedAttribute as CFString, &selectedSettable
    ) == .success, selectedSettable.boolValue,
       AXUIElementSetAttributeValue(
        element, kAXSelectedAttribute as CFString, kCFBooleanTrue
       ) == .success {
        return true
    }
    return false
}

func runSmoke() throws {
    guard AXIsProcessTrusted() else {
        throw SmokeFailure.message("ACCESSIBILITY_PERMISSION_REQUIRED")
    }
    guard let application = NSRunningApplication.runningApplications(
        withBundleIdentifier: "com.danielshayesteh.LiveInterviewCompanion"
    ).first else {
        throw SmokeFailure.message("APP_PROCESS_NOT_RUNNING")
    }
    application.activate()
    Thread.sleep(forTimeInterval: 0.5)

    let appElement = AXUIElementCreateApplication(application.processIdentifier)
    let windows = attribute(appElement, kAXWindowsAttribute) as? [AXUIElement] ?? []
    let permanentWindows = windows.filter {
        stringAttribute($0, kAXSubroleAttribute) == kAXStandardWindowSubrole
    }
    guard permanentWindows.count == 1, let mainWindow = permanentWindows.first else {
        throw SmokeFailure.message("EXPECTED_ONE_PERMANENT_WINDOW")
    }
    var sizeSettable = DarwinBoolean(false)
    guard AXUIElementIsAttributeSettable(
        mainWindow, kAXSizeAttribute as CFString, &sizeSettable
    ) == .success, sizeSettable.boolValue else {
        throw SmokeFailure.message("MAIN_WINDOW_NOT_RESIZABLE")
    }

    let sectionNames = [
        "Dashboard", "Applications", "Practice", "Live Interview",
        "Sessions", "Knowledge", "Settings",
    ]
    for sectionName in sectionNames {
        let rows = descendants(mainWindow).filter {
            stringAttribute($0, kAXRoleAttribute) == kAXRowRole
        }
        guard let row = rows.first(where: { accessibleStrings($0).contains(sectionName) }) else {
            throw SmokeFailure.message("AX_ROW_NOT_FOUND:\(sectionName)")
        }
        guard performPress(row) else {
            throw SmokeFailure.message("AX_ROW_NOT_PRESSABLE:\(sectionName)")
        }
        Thread.sleep(forTimeInterval: 0.25)
        guard boolAttribute(row, kAXSelectedAttribute) == true else {
            throw SmokeFailure.message("AX_ROW_NOT_SELECTED:\(sectionName)")
        }
        let currentWindows = attribute(appElement, kAXWindowsAttribute) as? [AXUIElement] ?? []
        let currentPermanentCount = currentWindows.filter {
            stringAttribute($0, kAXSubroleAttribute) == kAXStandardWindowSubrole
        }.count
        guard currentPermanentCount == 1 else {
            throw SmokeFailure.message("EXPECTED_ONE_PERMANENT_WINDOW_AFTER_NAVIGATION:\(sectionName)")
        }
    }

    let finalRows = descendants(mainWindow).filter {
        stringAttribute($0, kAXRoleAttribute) == kAXRowRole
    }
    guard let practice = finalRows.first(where: { accessibleStrings($0).contains("Practice") }),
          performPress(practice) else {
        throw SmokeFailure.message("PRACTICE_RETURN_FAILED")
    }
    print("F003_AX_SMOKE_PASSED")
}

do {
    try runSmoke()
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(2)
}
