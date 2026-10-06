package com.brahma.connect.core

/**
 * Canonical capability identifiers advertised by the Android agent.
 *
 * Keep these values aligned with brahma_connect.gateway.command_router.ACTION_CAPABILITIES.
 */
object BrahmaConnectCapabilities {
    const val DEVICE_INFO = "device_info"
    const val BATTERY = "battery"
    const val FLASHLIGHT = "flashlight"
    const val APP_LAUNCH = "app_launch"
    const val OPEN_URL = "open_url"
    const val VOLUME_CONTROL = "volume_control"
    const val UNLOCK_PHONE = "unlock_phone"
    const val FILES = "files"
    const val UI_CONTROL = "ui_control"

    val INITIAL: List<String> = listOf(
        DEVICE_INFO,
        BATTERY,
        FLASHLIGHT,
        APP_LAUNCH,
        OPEN_URL,
        VOLUME_CONTROL,
        UNLOCK_PHONE,
        FILES,
        UI_CONTROL,
    )
}
