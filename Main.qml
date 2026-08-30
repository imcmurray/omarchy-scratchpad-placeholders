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

  readonly property int restorableCount: {
    var n = 0
    for (var i = 0; i < root.placeholders.length; i++)
      if (root.placeholders[i].command) n++
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
    color: "transparent"
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
        text: "Click to start an app you kept here. Right-click to forget."
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
        color: Util.alpha(Color.popups.text, restoreAllArea.containsMouse && !restoreAllProcess.running ? 0.24 : 0.12)
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

      Text {
        width: parent.width
        visible: root.layoutFrozen
        text: "Layout tracking is paused while an app is missing — moves and "
              + "resizes are not saved. Restore first, then arrange."
        textFormat: Text.PlainText
        color: Util.alpha(Color.popups.text, 0.55)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
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
                text: modelData.name || "App"
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
                text: "Start"
                textFormat: Text.PlainText
                color: Util.alpha(Color.popups.text, 0.55)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                horizontalAlignment: Text.AlignHCenter
              }
            }

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              cursorShape: Qt.PointingHandCursor
              onClicked: function(mouse) {
                if (mouse.button === Qt.RightButton)
                  root.forgetApp(modelData.id || modelData.class)
                else if (modelData.command)
                  root.launchApp(modelData.id || modelData.class)
              }
            }
          }
        }
      }
    }
  }
}
