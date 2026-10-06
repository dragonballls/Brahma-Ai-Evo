package com.brahma.connect.pairing

import android.content.Context
import android.os.Build
import com.brahma.connect.core.DeviceCredential
import com.brahma.connect.core.PairingOffer
import org.json.JSONObject
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

class PairingStorage(context: Context) {
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "brahma_connect_secure",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun saveCredential(credential: DeviceCredential) {
        val committed = prefs.edit()
            .putString("device_credential", JSONObject()
                .put("device_id", credential.deviceId)
                .put("device_secret", credential.deviceSecret)
                .put("device_name", credential.deviceName)
                .put("gateway_host", credential.gatewayHost)
                .put("gateway_port", credential.gatewayPort)
                .put("tls", credential.tls)
                .put("tls_certificate_sha256", credential.tlsCertificateSha256)
                .put("paired_at", credential.pairedAt)
                .toString())
            .commit()
        check(committed) { "Failed to persist device credentials safely." }
    }

    fun loadCredential(): DeviceCredential? {
        val raw = prefs.getString("device_credential", null) ?: return null
        return try {
            val json = JSONObject(raw)
            val deviceId = json.optString("device_id").trim()
            val deviceSecret = json.optString("device_secret").trim()
            val gatewayHost = json.optString("gateway_host").trim()
            val gatewayPort = json.optInt("gateway_port", -1)
            if (deviceId.isBlank() || deviceSecret.isBlank() || gatewayHost.isBlank() || gatewayPort !in 1..65535) {
                throw IllegalArgumentException("Stored device credentials are incomplete.")
            }
            DeviceCredential(
                deviceId = deviceId,
                deviceSecret = deviceSecret,
                deviceName = json.optString("device_name", Build.MODEL),
                gatewayHost = gatewayHost,
                gatewayPort = gatewayPort,
                tls = json.optBoolean("tls", true),
                tlsCertificateSha256 = json.optString("tls_certificate_sha256"),
                pairedAt = json.optString("paired_at"),
            )
        } catch (exc: Exception) {
            throw IllegalStateException("Stored device credentials are corrupt; reconnect requires explicit repair.", exc)
        }
    }

    fun clearCredential() {
        check(prefs.edit().remove("device_credential").commit()) {
            "Failed to clear stored device credentials."
        }
    }

    fun saveGatewayHint(offer: PairingOffer) {
        check(
            prefs.edit()
                .putString("last_pairing_offer", offer.toJson().toString())
                .commit()
        ) { "Failed to persist gateway pairing hint safely." }
    }

    fun loadGatewayHint(): PairingOffer? {
        val raw = prefs.getString("last_pairing_offer", null) ?: return null
        return try {
            PairingOffer.fromJson(JSONObject(raw))
        } catch (exc: Exception) {
            throw IllegalStateException(
                "Stored gateway pairing hint is corrupt; refusing to treat it as absent.",
                exc,
            )
        }
    }
}
