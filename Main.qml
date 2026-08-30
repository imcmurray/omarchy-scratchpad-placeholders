import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false
  property var placeholders: []
  property bool layoutFrozen: false

  readonly property string missingSummary: {
    var names = []
    for (var i = 0; i < root.placeholders.length; i++)
      names.push(root.placeholders[i].label || root.placeholders[i].name || "App")
    if (names.length === 0) return ""
    if (names.length === 1) return names[0] + " is missing"
    if (names.length <= 3) return names.slice(0, -1).join(", ") + " and "
                                  + names[names.length - 1] + " are missing"
    return names.length + " apps are missing"
  }

  readonly property int restorableCount: {
    var n = 0
    for (var i = 0; i < root.placeholders.length; i++)
      if (root.placeholders[i].restorable) n++
    return n
  }

  readonly property string pluginDir: {
    if (root.manifest && root.manifest.__sourceDir)
      return String(root.manifest.__sourceDir).replace(/\/$/, "")
    var url = Qt.resolvedUrl(".").toString()
    if (url.indexOf("file://") === 0)
      url = decodeURIComponent(url.substring(7))
    return url.replace(/\/$/, "")
  }
  readonly property string layoutScript: root.pluginDir + "/layout.py"

  function open(payloadJson) {}
  function close() { root.opened = false }
  function toggle() {}

  function refresh() {
    if (!statusProcess.running) statusProcess.running = true
  }

  function launchApp(appId) {
    Quickshell.execDetached(["python3", root.layoutScript, "launch", appId])
    refreshSoon.restart()
  }

  function restoreAll() {
    if (!restoreAllProcess.running) restoreAllProcess.running = true
  }

  function forgetApp(appId) {
    Quickshell.execDetached(["python3", root.layoutScript, "forget", appId])
    refreshSoon.restart()
  }

  function applyStatus(raw) {
    try {
      var parsed = JSON.parse(String(raw || "{}"))
      var next = parsed.placeholders || []
      root.placeholders = next
      root.layoutFrozen = parsed.layoutFrozen === true
      root.opened = parsed.visible === true && next.length > 0
    } catch (e) {
      console.warn("ianm.scratchpad", "bad status", e)
    }
  }

  Component.onCompleted: refresh()

  Timer {
    id: refreshSoon
    interval: 250
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    interval: 5000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Connections {
    target: Hyprland
    function onRawEvent(event) {
      var name = String(event && event.name ? event.name : "")
      if (name === "openwindow" || name === "closewindow" || name === "movewindow"
          || name === "movewindowv2" || name === "changefloatingmode"
          || name === "activespecial" || name === "togglespecialworkspace"
          || name === "workspace")
        refreshSoon.restart()
    }
  }

  Process {
    id: restoreAllProcess
    running: false
    command: ["python3", root.layoutScript, "restore-all"]
    onExited: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    command: ["python3", root.layoutScript, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    // Without this the title, caption and footnote are alpha text on whatever
    // wallpaper happens to be up, and legibility is pot luck.
    color: Util.alpha(Color.background, 0.62)
    anchors { top: true; bottom: true; left: true; right: true }
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "omarchy-scratchpad-placeholders"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

    Column {
      anchors.centerIn: parent
      spacing: Style.space(18)
      width: Math.min(Style.space(720), panel.width - Style.gapsOut * 4)

      Text {
        width: parent.width
        text: "Scratchpad"
        textFormat: Text.PlainText
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.bold: true
        horizontalAlignment: Text.AlignHCenter
      }

      Text {
        width: parent.width
        text: "Right-click a tile to forget it."
        textFormat: Text.PlainText
        color: Util.alpha(Color.popups.text, 0.7)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
      }

      BorderSurface {
        id: restoreAll
        visible: root.restorableCount > 1
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(parent.width, Style.space(300))
        height: Style.space(46)
        color: restoreAllProcess.running ? Util.alpha(Color.accent, 0.10)
             : restoreAllArea.pressed     ? Util.alpha(Color.accent, 0.52)
             : restoreAllArea.containsMouse ? Util.alpha(Color.accent, 0.44)
             : Util.alpha(Color.accent, 0.30)
        borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
        radius: Style.cornerRadius

        Text {
          anchors.centerIn: parent
          text: restoreAllProcess.running
                ? "Restoring…"
                : "Restore all " + root.restorableCount + " apps"
          textFormat: Text.PlainText
          color: Util.alpha(Color.popups.text, restoreAllProcess.running ? 0.6 : 1.0)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          font.bold: true
        }

        MouseArea {
          id: restoreAllArea
          anchors.fill: parent
          hoverEnabled: true
          enabled: !restoreAllProcess.running
          cursorShape: Qt.PointingHandCursor
          onClicked: root.restoreAll()
        }
      }

      Flow {
        id: tiles
        width: parent.width
        spacing: Style.space(14)
        property real tileWidth: Style.space(150)

        Repeater {
          model: root.placeholders

          BorderSurface {
            required property var modelData

            width: tiles.tileWidth
            height: Style.space(150)
            color: Util.alpha(Color.popups.background, 0.92)
            borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
            radius: Style.cornerRadius

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              visible: tileArea.containsMouse
              color: tileArea.pressed ? Style.pressedFillFor(Color.popups.text, Color.accent)
                                      : Style.hoverFillFor(Color.popups.text, Color.accent)
            }

            Column {
              anchors.fill: parent
              anchors.margins: Style.space(14)
              spacing: Style.space(10)

              Item {
                width: parent.width
                height: Style.space(56)

                Image {
                  anchors.centerIn: parent
                  width: Style.space(48)
                  height: Style.space(48)
                  source: modelData.icon ? Util.fileUrl(modelData.icon) : ""
                  fillMode: Image.PreserveAspectFit
                  visible: status === Image.Ready
                }

                Text {
                  anchors.centerIn: parent
                  visible: parent.children[0].status !== Image.Ready
                  text: modelData.glyph || "󰣆"
                  textFormat: Text.PlainText
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.display
                }
              }

              Text {
                width: parent.width
                text: modelData.label || modelData.name || "App"
                textFormat: Text.PlainText
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: !modelData.restorable
                      ? "Can't be started"
                      : (modelData.w > 0 && modelData.h > 0)
                        ? modelData.w + " × " + modelData.h
                        : "Start"
                textFormat: Text.PlainText
                color: modelData.restorable ? Util.alpha(Color.popups.text, 0.55)
                                            : Util.alpha(Color.urgent, 0.95)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                horizontalAlignment: Text.AlignHCenter
              }
            }

            Rectangle {
              id: dismiss
              anchors { top: parent.top; right: parent.right; margins: Style.space(6) }
              width: Style.space(22)
              height: Style.space(22)
              radius: width / 2
              z: 2
              visible: tileArea.containsMouse || dismissArea.containsMouse
                       || !modelData.restorable
              color: dismissArea.containsMouse ? Util.alpha(Color.urgent, 0.85)
                                               : Util.alpha(Color.popups.text, 0.18)

              Text {
                anchors.centerIn: parent
                text: "×"
                textFormat: Text.PlainText
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }

              MouseArea {
                id: dismissArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.forgetApp(modelData.id || modelData.class)
              }
            }

            MouseArea {
              id: tileArea
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              cursorShape: Qt.PointingHandCursor
              onClicked: function(mouse) {
                if (mouse.button === Qt.RightButton)
                  root.forgetApp(modelData.id || modelData.class)
                else if (modelData.restorable)
                  root.launchApp(modelData.id || modelData.class)
              }
            }
          }
        }
      }

      Text {
        width: parent.width
        visible: root.layoutFrozen
        text: root.missingSummary + " — moves aren't saved until every app is "
              + "back. Dismiss a tile with × if you don't want it any more."
        textFormat: Text.PlainText
        color: Util.alpha(Color.popups.text, 0.5)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
      }
    }
  }
}
