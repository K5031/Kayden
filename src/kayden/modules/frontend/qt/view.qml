import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    width: backend.windowWidth
    height: backend.windowHeight
    minimumWidth: 480
    minimumHeight: 420
    visible: true
    title: backend.assistantName
    color: "#171717"
    palette.window: "#171717"
    palette.windowText: "#ececec"
    palette.base: "#111111"
    palette.text: "#ececec"
    palette.button: "#2f2f2f"
    palette.buttonText: "#ececec"
    palette.highlight: "#8ab4f8"
    palette.highlightedText: "#171717"

    property bool busy: false
    property bool following: true
    property int activeAssistant: -1
    property int activeToolGroup: -1
    property int activeToolGroupId: -1
    property string captureState: "idle"
    property bool captureAvailable: backend.captureAvailable
    property string captureLabel: backend.captureLabel
    property string statusText: "Ready"
    property var moduleCatalog: backend.modules
    property var controlCatalog: backend.controls
    property var moduleDraft: ({})
    property bool moduleApplying: false
    property string moduleError: ""
    property real pageMargin: Math.min(80, Math.max(20, width * 0.052))
    property real feedWidth: width - pageMargin * 2
    property real assistantWidth: Math.min(1180, feedWidth)

    component NightButton: Button {
        id: control
        implicitHeight: 38
        leftPadding: 16
        rightPadding: 16
        contentItem: Text {
            text: control.text
            color: control.enabled ? "#ececec" : "#737373"
            font.pixelSize: 13
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 9
            color: control.down ? "#404040"
                : control.hovered ? "#292929" : "#2f2f2f"
            border.width: 1
            border.color: control.activeFocus ? "#8ab4f8" : "#404040"
            opacity: control.enabled ? 1 : 0.55
        }
    }

    function revealLatest() {
        if (following)
            Qt.callLater(function() { transcript.positionViewAtEnd() })
    }

    function addStatus(message) {
        statusText = message
        if (message.indexOf("[tool]") === 0) {
            transcriptModel.append({
                "kind": "tool",
                "content": message.substring(6).trim(),
                "interrupted": false,
                "callId": "",
                "toolState": "finished",
                "toolDetails": "",
                "toolExpanded": false,
                "toolCount": 1
            })
            revealLatest()
        } else if (message.indexOf("[error]") === 0) {
            transcriptModel.append({
                "kind": "error",
                "content": message.substring(7).trim(),
                "interrupted": false,
                "callId": "",
                "toolState": "failed",
                "toolDetails": "",
                "toolExpanded": false,
                "toolCount": 0
            })
            revealLatest()
        }
    }

    function updateToolGroup(groupId, summary, state, details, count) {
        activeAssistant = -1
        if (activeToolGroup < 0 || activeToolGroupId !== groupId) {
            transcriptModel.append({
                "kind": "tool",
                "content": summary,
                "interrupted": false,
                "callId": "",
                "toolState": state,
                "toolDetails": details,
                "toolExpanded": false,
                "toolCount": count
            })
            activeToolGroup = transcriptModel.count - 1
            activeToolGroupId = groupId
        } else {
            transcriptModel.setProperty(activeToolGroup, "content", summary)
            transcriptModel.setProperty(activeToolGroup, "toolState", state)
            transcriptModel.setProperty(activeToolGroup, "toolDetails", details)
            transcriptModel.setProperty(activeToolGroup, "toolCount", count)
        }
        revealLatest()
    }

    function submitDraft() {
        var message = composer.text.trim()
        if (!message)
            return
        backend.submit(message)
        composer.clear()
    }

    function resetModuleDraft() {
        var draft = {}
        for (var index = 0; index < moduleCatalog.length; index++) {
            var category = moduleCatalog[index]
            var selected = []
            for (var selectedIndex = 0;
                    selectedIndex < category.selected.length; selectedIndex++)
                selected.push(category.selected[selectedIndex])
            draft[category.id] = selected
        }
        moduleDraft = draft
        moduleError = ""
    }

    function moduleSelected(capability, moduleName) {
        var selected = moduleDraft[capability] || []
        return selected.indexOf(moduleName) >= 0
    }

    function setModuleSelected(capability, moduleName, multiple) {
        var current = moduleDraft[capability] || []
        var selected = []
        for (var index = 0; index < current.length; index++)
            selected.push(current[index])
        var existing = selected.indexOf(moduleName)
        if (multiple) {
            if (existing >= 0)
                selected.splice(existing, 1)
            else
                selected.push(moduleName)
        } else {
            selected = [moduleName]
        }
        var next = {}
        for (var key in moduleDraft)
            next[key] = moduleDraft[key]
        next[capability] = selected
        moduleDraft = next
    }

    function disableOptionalModule(capability) {
        var next = {}
        for (var key in moduleDraft)
            next[key] = moduleDraft[key]
        next[capability] = []
        moduleDraft = next
    }

    Component.onCompleted: resetModuleDraft()

    onClosing: backend.close()

    ListModel { id: transcriptModel }

    Connections {
        target: backend

        function onUserReceived(message) {
            transcriptModel.append({
                "kind": "user", "content": message, "interrupted": false,
                "callId": "", "toolState": "", "toolDetails": "",
                "toolExpanded": false, "toolCount": 0
            })
            root.activeAssistant = -1
            root.activeToolGroup = -1
            root.activeToolGroupId = -1
            root.busy = true
            root.statusText = "Thinking…"
            root.following = true
            root.revealLatest()
        }

        function onAssistantReceived(token) {
            if (root.activeAssistant < 0) {
                transcriptModel.append({
                    "kind": "assistant", "content": "", "interrupted": false,
                    "callId": "", "toolState": "", "toolDetails": "",
                    "toolExpanded": false, "toolCount": 0
                })
                root.activeAssistant = transcriptModel.count - 1
            }
            var current = transcriptModel.get(root.activeAssistant).content
            transcriptModel.setProperty(root.activeAssistant, "content", current + token)
            root.revealLatest()
        }

        function onStatusReceived(message) {
            root.addStatus(message)
        }

        function onToolGroupReceived(groupId, summary, state, details, count) {
            root.statusText = state === "running" || state === "failed"
                ? summary : "Thinking…"
            root.updateToolGroup(groupId, summary, state, details, count)
        }

        function onFinishReceived(interrupted) {
            if (root.activeAssistant >= 0)
                transcriptModel.setProperty(root.activeAssistant, "interrupted", interrupted)
            root.activeAssistant = -1
            root.busy = false
            root.statusText = interrupted ? "Interrupted" : "Ready"
        }

        function onCaptureStateReceived(state) {
            root.captureState = state
            root.statusText = state === "recording" ? "Recording…"
                : state === "transcribing" ? "Transcribing…" : "Ready"
        }

        function onCaptureConfigured(available, label) {
            root.captureAvailable = available
            root.captureLabel = label
            if (!available)
                root.captureState = "idle"
        }

        function onDraftReceived(text) {
            var spacer = composer.text.length > 0 && !composer.text.endsWith(" ") ? " " : ""
            composer.text += spacer + text
            composer.forceActiveFocus()
            composer.cursorPosition = composer.length
        }

        function onModelReceived(model, label) {
            for (var index = 0; index < modelPicker.count; index++) {
                if (modelPicker.valueAt(index) === model) {
                    modelPicker.currentIndex = index
                    break
                }
            }
            root.statusText = "Model: " + label
        }

        function onModulesReceived(modules) {
            root.moduleCatalog = modules
            root.resetModuleDraft()
            if (moduleDialog.visible)
                moduleDialog.close()
        }

        function onModulesBusyReceived(busy, message) {
            root.moduleApplying = busy
            root.moduleError = message
            if (message.length > 0)
                root.statusText = "Module change failed"
        }

        function onControlsReceived(controls) {
            root.controlCatalog = controls
        }
    }

    header: Rectangle {
        height: 68
        color: "#111111"
        border.color: "#292929"

        Column {
            anchors.left: parent.left
            anchors.leftMargin: root.pageMargin
            anchors.verticalCenter: parent.verticalCenter
            spacing: 3

            Label {
                text: backend.assistantName
                color: "#ececec"
                font.pixelSize: 17
                font.bold: true
            }
            Label {
                text: root.statusText
                color: root.captureState === "recording" ? "#f87171" : "#8ab4f8"
                font.pixelSize: 11
            }
        }

        Row {
            anchors.right: parent.right
            anchors.rightMargin: root.pageMargin
            anchors.verticalCenter: parent.verticalCenter
            spacing: 10

            Repeater {
                model: root.controlCatalog

                delegate: NightButton {
                    required property var modelData
                    visible: modelData.placement === "header"
                        && (modelData.kind === "button"
                            || modelData.kind === "toggle")
                    enabled: modelData.enabled && !root.moduleApplying
                    text: modelData.label
                    onClicked: backend.invokeControl(
                        modelData.id,
                        modelData.kind === "toggle" ? !modelData.value : null
                    )
                }
            }

            NightButton {
                visible: root.moduleCatalog.length > 0
                enabled: !root.busy && !root.moduleApplying
                text: "Modules"
                onClicked: {
                    root.resetModuleDraft()
                    moduleDialog.open()
                }
            }

            ComboBox {
                id: modelPicker
                objectName: "modelPicker"
                width: Math.max(150, Math.min(210, root.width * 0.24))
                height: 38
                visible: count > 1
                enabled: !root.busy
                model: backend.models
                textRole: "name"
                valueRole: "id"
                leftPadding: 13
                rightPadding: 34

                contentItem: Text {
                    text: modelPicker.displayText
                    color: modelPicker.enabled ? "#ececec" : "#737373"
                    font.pixelSize: 13
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                indicator: Text {
                    x: modelPicker.width - width - 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: "⌄"
                    color: "#8ab4f8"
                    font.pixelSize: 17
                }
                background: Rectangle {
                    radius: 9
                    color: modelPicker.hovered ? "#292929" : "#2f2f2f"
                    border.width: 1
                    border.color: modelPicker.activeFocus ? "#8ab4f8" : "#404040"
                    opacity: modelPicker.enabled ? 1 : 0.6
                }
                delegate: ItemDelegate {
                    id: modelOption
                    required property int index
                    width: modelPicker.width
                    height: 40
                    text: modelPicker.textAt(index)
                    highlighted: modelPicker.highlightedIndex === index

                    contentItem: Text {
                        text: modelOption.text
                        color: "#ececec"
                        font.pixelSize: 13
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    background: Rectangle {
                        color: modelOption.highlighted ? "#404040"
                            : modelOption.hovered ? "#292929" : "#111111"
                    }
                }

                Component.onCompleted: {
                    for (var index = 0; index < count; index++) {
                        if (valueAt(index) === backend.selectedModel) {
                            currentIndex = index
                            break
                        }
                    }
                }
                onActivated: backend.selectModel(currentValue)
            }

            NightButton {
                visible: root.busy
                text: "Stop"
                onClicked: backend.interrupt()
            }
        }
    }

    Popup {
        id: moduleDialog
        objectName: "moduleDialog"
        anchors.centerIn: Overlay.overlay
        width: Math.min(root.width - 40, 720)
        height: Math.min(root.height - 40, 680)
        modal: true
        focus: true
        closePolicy: root.moduleApplying ? Popup.NoAutoClose
            : Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: 0

        background: Rectangle {
            radius: 14
            color: "#111111"
            border.width: 1
            border.color: "#404040"
        }

        contentItem: ColumnLayout {
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 16
                Layout.topMargin: 16
                Layout.bottomMargin: 14

                ColumnLayout {
                    spacing: 3
                    Label {
                        text: "Runtime modules"
                        color: "#ececec"
                        font.pixelSize: 18
                        font.bold: true
                    }
                    Label {
                        text: "Changes activate together at a safe turn boundary."
                        color: "#8a8a8a"
                        font.pixelSize: 12
                    }
                }

                Item { Layout.fillWidth: true }
                NightButton {
                    text: "Close"
                    enabled: !root.moduleApplying
                    onClicked: moduleDialog.close()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: "#292929"
            }

            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                Column {
                    width: moduleDialog.availableWidth
                    padding: 22
                    spacing: 22

                    Repeater {
                        model: root.moduleCatalog

                        delegate: Column {
                            id: categoryBlock
                            required property var modelData
                            property var category: modelData
                            width: parent.width - 44
                            spacing: 9

                            Label {
                                text: categoryBlock.category.name
                                color: "#d1d1d1"
                                font.pixelSize: 12
                                font.bold: true
                                font.capitalization: Font.AllUppercase
                            }

                            Flow {
                                width: parent.width
                                spacing: 8

                                NightButton {
                                    visible: categoryBlock.category.optional
                                        && !categoryBlock.category.multiple
                                    text: (root.moduleDraft[categoryBlock.category.id] || []).length === 0
                                        ? "✓ Off" : "Off"
                                    onClicked: root.disableOptionalModule(
                                        categoryBlock.category.id)
                                }

                                Repeater {
                                    model: categoryBlock.category.options

                                    delegate: NightButton {
                                        required property var modelData
                                        property var option: modelData
                                        text: (root.moduleSelected(
                                            categoryBlock.category.id,
                                            option.id) ? "✓ " : "") + option.name
                                        onClicked: root.setModuleSelected(
                                            categoryBlock.category.id,
                                            option.id,
                                            categoryBlock.category.multiple)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Label {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: visible ? 10 : 0
                visible: root.moduleError.length > 0
                text: root.moduleError
                color: "#f87171"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.topMargin: 12
                Layout.bottomMargin: 18

                Label {
                    text: root.moduleApplying ? "Preparing modules…" : ""
                    color: "#8ab4f8"
                    font.pixelSize: 12
                }
                Item { Layout.fillWidth: true }
                NightButton {
                    text: root.moduleApplying ? "Applying…" : "Apply"
                    enabled: !root.moduleApplying
                    onClicked: backend.applyModules(JSON.stringify(root.moduleDraft))
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: root.pageMargin
        anchors.rightMargin: root.pageMargin
        anchors.topMargin: 12
        anchors.bottomMargin: 16
        spacing: 14

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: transcript
                anchors.fill: parent
                clip: true
                spacing: 6
                model: transcriptModel
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                onMovementEnded: root.following = atYEnd

                delegate: Item {
                    id: messageRow
                    required property string kind
                    required property string content
                    required property bool interrupted
                    required property string callId
                    required property string toolState
                    required property string toolDetails
                    required property bool toolExpanded
                    required property int toolCount
                    required property int index
                    width: ListView.view.width
                    height: kind === "tool"
                        ? 34 + (toolExpanded && toolCount > 1
                            ? toolDetailsBody.contentHeight + 12 : 0)
                        : messageColumn.implicitHeight + 8
                    property real conversationWidth: Math.min(width,
                        kind === "user" ? root.feedWidth : root.assistantWidth)

                    TextMetrics {
                        id: userMetrics
                        text: messageRow.content
                        font.pixelSize: 15
                    }

                    Column {
                        id: messageColumn
                        width: kind === "user"
                            ? Math.min(messageRow.conversationWidth * 0.72,
                                Math.max(112, userMetrics.advanceWidth + 34))
                            : messageRow.conversationWidth
                        x: kind === "user" ? parent.width - width : 0

                        Rectangle {
                            width: parent.width
                            height: kind === "tool" ? messageRow.height - 4
                                : body.contentHeight + (kind === "assistant" ? 8 : 20)
                            radius: kind === "tool" ? 0 : 13
                            color: kind === "user" ? "#2f2f2f"
                                : kind === "tool" ? "transparent"
                                : kind === "error" ? "#30202b" : "transparent"
                            border.width: kind === "assistant" || kind === "tool" ? 0 : 1
                            border.color: kind === "error" ? "#f87171" : "#404040"

                            TextEdit {
                                id: body
                                visible: kind !== "tool"
                                anchors.fill: parent
                                anchors.margins: kind === "assistant" ? 4 : 10
                                readOnly: true
                                selectByMouse: true
                                wrapMode: TextEdit.Wrap
                                textFormat: kind === "assistant" ? TextEdit.RichText : TextEdit.PlainText
                                text: kind === "assistant"
                                    ? backend.renderMarkdown(content
                                        + (interrupted ? "\n\n*Interrupted*" : ""))
                                    : (kind === "error" ? "Error  " : "") + content
                                color: kind === "error" ? "#f87171" : "#ececec"
                                selectionColor: "#8ab4f8"
                                selectedTextColor: "#171717"
                                font.pixelSize: 15
                            }

                            Text {
                                id: toolSummary
                                visible: kind === "tool"
                                anchors.left: parent.left
                                anchors.right: toolChevron.left
                                anchors.top: parent.top
                                height: 30
                                anchors.leftMargin: 6
                                anchors.rightMargin: 6
                                text: (toolState === "running" ? "◌  "
                                    : toolState === "failed" ? "!  " : "✓  ") + content
                                color: toolState === "failed" ? "#f87171" : "#8ab4f8"
                                font.pixelSize: 14
                                elide: Text.ElideRight
                                maximumLineCount: 1
                            }

                            Text {
                                id: toolChevron
                                visible: kind === "tool" && toolCount > 1
                                anchors.right: parent.right
                                anchors.rightMargin: 8
                                anchors.top: parent.top
                                height: 30
                                verticalAlignment: Text.AlignVCenter
                                text: toolExpanded ? "⌃" : "⌄"
                                color: "#737373"
                                font.pixelSize: 14
                            }

                            Text {
                                id: toolDetailsBody
                                visible: kind === "tool" && toolExpanded && toolCount > 1
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.topMargin: 32
                                anchors.leftMargin: 27
                                anchors.rightMargin: 12
                                text: toolDetails
                                color: "#d1d1d1"
                                font.pixelSize: 13
                                lineHeight: 1.35
                                wrapMode: Text.Wrap
                            }

                            MouseArea {
                                visible: kind === "tool" && toolCount > 1
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                height: 30
                                cursorShape: Qt.PointingHandCursor
                                onClicked: transcriptModel.setProperty(
                                    messageRow.index,
                                    "toolExpanded",
                                    !messageRow.toolExpanded
                                )
                            }
                        }
                    }
                }
            }

            NightButton {
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                visible: !transcript.atYEnd && transcript.count > 0
                text: "Latest ↓"
                onClicked: {
                    root.following = true
                    transcript.positionViewAtEnd()
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredWidth: root.feedWidth
            Layout.maximumWidth: root.feedWidth
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredHeight: Math.max(68, Math.min(150, composer.contentHeight + 28))
            radius: 14
            color: "#111111"
            border.width: 1
            border.color: composer.activeFocus ? "#8ab4f8" : "#404040"

            RowLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 9

                TextArea {
                    id: composer
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    placeholderText: "Message " + backend.assistantName + "…"
                    color: "#ececec"
                    placeholderTextColor: "#737373"
                    wrapMode: TextArea.Wrap
                    background: null
                    font.pixelSize: 15

                    Keys.onPressed: function(event) {
                        if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                && !(event.modifiers & Qt.ShiftModifier)) {
                            root.submitDraft()
                            event.accepted = true
                        } else if (event.key === Qt.Key_Escape) {
                            if (root.captureState === "recording")
                                backend.cancelCapture()
                            else if (root.busy)
                                backend.interrupt()
                            event.accepted = true
                        }
                    }
                }

                Repeater {
                    model: root.controlCatalog

                    delegate: NightButton {
                        required property var modelData
                        visible: modelData.placement === "composer"
                            && (modelData.kind === "button"
                                || modelData.kind === "toggle")
                        enabled: modelData.enabled && !root.moduleApplying
                        text: modelData.label
                        onClicked: backend.invokeControl(
                            modelData.id,
                            modelData.kind === "toggle" ? !modelData.value : null
                        )
                    }
                }

                NightButton {
                    visible: root.captureAvailable
                    enabled: root.captureState !== "transcribing"
                    text: root.captureState === "recording" ? "Finish" : root.captureLabel
                    onClicked: root.captureState === "recording"
                        ? backend.finishCapture() : backend.startCapture()
                }

                NightButton {
                    text: "Send"
                    enabled: composer.text.trim().length > 0
                    onClicked: root.submitDraft()
                }
            }
        }

        Label {
            Layout.alignment: Qt.AlignHCenter
            text: "Enter to send  ·  Shift+Enter for a new line"
            color: "#737373"
            font.pixelSize: 11
        }
    }
}
