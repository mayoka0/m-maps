package org.mmaps.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.location.Location
import android.location.LocationManager
import android.os.IBinder
import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.ServerSocket
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import com.google.android.gms.location.LocationServices
import com.google.android.gms.tasks.Tasks
import org.json.JSONObject

/**
 * Prototype transport: M Maps forwards a localhost ADB port to this service.
 * Every line is one JSON command. No internet socket is opened by this service.
 */
class LocationService : Service() {
    private val executor = Executors.newSingleThreadExecutor()
    private val keepAliveExecutor = Executors.newSingleThreadScheduledExecutor()
    private var server: ServerSocket? = null
    @Volatile private var currentTarget: MockTarget? = null
    private val fusedClient by lazy { LocationServices.getFusedLocationProviderClient(this) }

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, notification())
        fusedClient.setMockMode(true).addOnFailureListener {
            Log.e(TAG, "Could not enable fused mock mode", it)
        }
        executor.execute { listen() }
        keepAliveExecutor.scheduleAtFixedRate(
            { currentTarget?.let(::publishMockLocation) },
            KEEPALIVE_SECONDS,
            KEEPALIVE_SECONDS,
            TimeUnit.SECONDS,
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Ask Android to recreate the foreground service if the process is
        // reclaimed. The last target lives only in memory, so a recreated
        // service waits safely for the Mac to resend it.
        return START_STICKY
    }

    private fun listen() {
        try {
            server = ServerSocket(PORT, 1, java.net.InetAddress.getByName("127.0.0.1"))
            Log.i(TAG, "Listening on localhost:$PORT")
            while (!server!!.isClosed) {
                server!!.accept().use { socket ->
                    val writer = PrintWriter(socket.getOutputStream(), true)
                    BufferedReader(InputStreamReader(socket.getInputStream())).forEachLine { line ->
                        val accepted = try {
                            handle(JSONObject(line))
                        } catch (error: Exception) {
                            Log.e(TAG, "Invalid companion command", error)
                            false
                        }
                        writer.println("{\"ok\":$accepted}")
                    }
                }
            }
        } catch (error: Exception) {
            // A disconnected ADB forward closes the socket; cleanup is handled below.
            Log.e(TAG, "Location socket stopped", error)
            clearMockLocation()
        }
    }

    private fun handle(command: JSONObject): Boolean {
        return when (command.optString("action")) {
            "PING" -> true
            "SET_LOCATION" -> setMockLocation(command)
            "CLEAR_LOCATION", "STOP_SERVICE" -> clearMockLocationAcknowledged()
            else -> false
        }
    }

    private fun setMockLocation(command: JSONObject): Boolean {
        return try {
            val target = MockTarget(
                latitude = command.getDouble("latitude"),
                longitude = command.getDouble("longitude"),
                altitude = command.optDouble("altitude", 0.0),
                speed = command.optDouble("speed", 0.0).toFloat(),
                bearing = command.optDouble("bearing", 0.0).toFloat(),
                accuracy = command.optDouble("accuracy", 5.0).toFloat(),
            )
            val task = fusedClient.setMockMode(true).continueWithTask {
                fusedClient.setMockLocation(locationFor(target))
            }
            Tasks.await(task, COMMAND_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            currentTarget = target
            Log.i(TAG, "Published ${target.latitude},${target.longitude}")
            true
        } catch (error: Exception) {
            // The user must select this APK as the active mock-location app.
            Log.e(TAG, "Could not publish mock location", error)
            false
        }
    }

    /** Refresh the timestamp so Android never considers the held mock location stale. */
    private fun publishMockLocation(target: MockTarget) {
        val location = locationFor(target)
        fusedClient.setMockMode(true).continueWithTask {
            fusedClient.setMockLocation(location)
        }.addOnFailureListener {
            Log.e(TAG, "Could not refresh mock location", it)
        }
    }

    private fun locationFor(target: MockTarget): Location {
        return Location(LocationManager.GPS_PROVIDER).apply {
            latitude = target.latitude
            longitude = target.longitude
            altitude = target.altitude
            speed = target.speed
            bearing = target.bearing
            accuracy = target.accuracy
            time = System.currentTimeMillis()
            elapsedRealtimeNanos = android.os.SystemClock.elapsedRealtimeNanos()
        }
    }

    private fun clearMockLocationAcknowledged(): Boolean {
        return try {
            Tasks.await(
                fusedClient.setMockMode(false),
                COMMAND_TIMEOUT_SECONDS,
                TimeUnit.SECONDS,
            )
            currentTarget = null
            true
        } catch (error: Exception) {
            Log.e(TAG, "Could not clear mock location", error)
            false
        }
    }

    private fun clearMockLocation() {
        currentTarget = null
        try {
            fusedClient.setMockMode(false)
        } catch (_: Exception) {
            // Cleanup is best-effort; the next service start retries it.
        }
    }

    private fun notification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Location simulation", NotificationManager.IMPORTANCE_LOW))
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("M Maps Companion")
            .setContentText("Mock location service is ready")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .build()
    }

    override fun onDestroy() {
        server?.close()
        clearMockLocation()
        executor.shutdownNow()
        keepAliveExecutor.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "MMapsCompanion"
        private const val PORT = 8765
        private const val CHANNEL = "mmaps_location"
        private const val NOTIFICATION_ID = 1
        private const val KEEPALIVE_SECONDS = 3L
        private const val COMMAND_TIMEOUT_SECONDS = 3L
    }

    private data class MockTarget(
        val latitude: Double,
        val longitude: Double,
        val altitude: Double,
        val speed: Float,
        val bearing: Float,
        val accuracy: Float,
    )
}
