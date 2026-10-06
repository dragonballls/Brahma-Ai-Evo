package com.brahma.connect.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.brahma.connect.core.AgentStateStore

class BrahmaAccessibilityService : AccessibilityService() {
    companion object {
        const val MAX_UI_NODES = 1000
        const val MAX_UI_DEPTH = 100
        const val MAX_UI_TEXT_CHARS = 512
        const val MAX_GESTURE_DURATION_MS = 10_000L

        var instance: BrahmaAccessibilityService? = null
    }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Intentionally left blank for now.
    }

    override fun onInterrupt() {
        // Intentionally left blank for now.
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        AgentStateStore.addLog("Accessibility service enabled.")
        Companion.instance = this
    }
    
    fun unlockPhone(pin: String): Boolean {
        // Remote PIN entry is intentionally not implemented. Never report an
        // unlock as successful when no device-side unlock operation occurred.
        AgentStateStore.addLog("Remote phone unlock is not implemented.")
        return false
    }

    fun dumpUiTree(): Map<String, Any> {
        val root = rootInActiveWindow
        if (root == null) {
            AgentStateStore.addLog("UI dump failed: No active window.")
            return mapOf("error" to "No active window")
        }
        val nodes = mutableListOf<Map<String, Any>>()
        if (!traverseNode(root, nodes, 0)) {
            AgentStateStore.addLog("UI dump failed: UI tree exceeded safety limits.")
            return mapOf("error" to "UI tree exceeds safety limits")
        }
        return mapOf(
            "screen_width" to resources.displayMetrics.widthPixels,
            "screen_height" to resources.displayMetrics.heightPixels,
            "nodes" to nodes,
        )
    }

    private fun traverseNode(
        node: AccessibilityNodeInfo,
        nodes: MutableList<Map<String, Any>>,
        depth: Int,
    ): Boolean {
        if (depth > MAX_UI_DEPTH || nodes.size >= MAX_UI_NODES) return false

        if (node.isVisibleToUser) {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            
            val nodeMap = mutableMapOf<String, Any>(
                "class" to (node.className?.toString() ?: "").take(MAX_UI_TEXT_CHARS),
                "text" to (node.text?.toString() ?: "").take(MAX_UI_TEXT_CHARS),
                "content_description" to (node.contentDescription?.toString() ?: "").take(MAX_UI_TEXT_CHARS),
                "bounds" to listOf(bounds.left, bounds.top, bounds.right, bounds.bottom),
                "is_clickable" to node.isClickable,
                "is_scrollable" to node.isScrollable,
                "is_focused" to node.isFocused
            )
            nodes.add(nodeMap)
        }

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val accepted = try {
                traverseNode(child, nodes, depth + 1)
            } finally {
                child.recycle()
            }
            if (!accepted) return false
        }
        return true
    }

    private fun withinScreen(x: Int, y: Int): Boolean {
        val width = resources.displayMetrics.widthPixels
        val height = resources.displayMetrics.heightPixels
        return x in 0 until width && y in 0 until height
    }

    fun tap(x: Int, y: Int): Boolean {
        if (!withinScreen(x, y)) return false
        val path = Path()
        path.moveTo(x.toFloat(), y.toFloat())
        val gestureBuilder = GestureDescription.Builder()
        gestureBuilder.addStroke(GestureDescription.StrokeDescription(path, 0, 50))
        return dispatchGesture(gestureBuilder.build(), null, null)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, duration: Long): Boolean {
        if (!withinScreen(x1, y1) || !withinScreen(x2, y2)) return false
        if (duration !in 1L..MAX_GESTURE_DURATION_MS) return false
        val path = Path()
        path.moveTo(x1.toFloat(), y1.toFloat())
        path.lineTo(x2.toFloat(), y2.toFloat())
        val gestureBuilder = GestureDescription.Builder()
        gestureBuilder.addStroke(GestureDescription.StrokeDescription(path, 0, duration))
        return dispatchGesture(gestureBuilder.build(), null, null)
    }

    fun typeText(text: String): Boolean {
        val root = rootInActiveWindow ?: return false
        val focusedNode = findFocusedNode(root)
        if (focusedNode != null) {
            val arguments = Bundle()
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            return focusedNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
        }
        return false
    }

    private fun findFocusedNode(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isFocused) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i)
            if (child != null) {
                val found = findFocusedNode(child)
                if (found != null) return found
                child.recycle()
            }
        }
        return null
    }

}
