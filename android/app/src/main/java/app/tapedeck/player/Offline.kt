package app.tapedeck.player

import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.cache.NoOpCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.offline.Download
import androidx.media3.exoplayer.offline.DownloadManager
import androidx.media3.exoplayer.offline.DownloadNotificationHelper
import androidx.media3.exoplayer.offline.DownloadRequest
import androidx.media3.exoplayer.offline.DownloadService
import androidx.media3.exoplayer.scheduler.Requirements
import androidx.media3.exoplayer.scheduler.Scheduler
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody

/** What the saving progress bar shows. */
data class SaveStatus(
    val saved: Int = 0,
    val total: Int = 0,
    val progress: Float = 0f,
    val failed: Int = 0,
    val bytes: Long = 0L,
    val waitingForWifi: Boolean = false,
    val waitingForNetwork: Boolean = false,
    val enabled: Boolean = true,
    val active: Boolean = false,
    val bytesPerSecond: Double = 0.0,
    val fromComputer: Boolean = false,
)

/**
 * Saves every song in the library onto the phone, so it plays offline.
 *
 * Saved songs and played songs share one store, keyed by each song's YouTube link. The player
 * reads from it first, so a saved song never touches the network. Saving runs in a foreground
 * service with a progress notification, on Wi-Fi unless mobile data is allowed, and picks up
 * where it left off after the app or the phone restarts.
 *
 * While the computer's Copy to your phone window is open, songs saved there are copied
 * straight from it over Wi-Fi; if it goes away, those songs come from YouTube instead.
 */
@OptIn(UnstableApi::class)
object Offline {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var databaseInstance: StandaloneDatabaseProvider? = null
    private var cacheInstance: SimpleCache? = null
    private var managerInstance: DownloadManager? = null
    private var preferencesInstance: SharedPreferences? = null
    private var appContext: Context? = null
    private var firstSync = true

    /** The computer's transfer, while its window is open: where to ask, and what it has. */
    private class Computer(val base: String, val token: String, val songs: Set<String>)

    @Volatile
    private var computer: Computer? = null

    @Volatile
    private var computerCheckedAt = 0L
    private val fromComputer: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private val computerFailed: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private val quick = YouTube.http.newBuilder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    // Download speed: bytes seen per download at the last sample, smoothed over a few seconds.
    private val lastBytes = HashMap<String, Long>()
    private var lastSampleAt = 0L
    private var bytesPerSecond = 0.0

    @Volatile
    private var wanted: Set<String> = emptySet()

    private val _status = MutableStateFlow(SaveStatus())
    val status: StateFlow<SaveStatus> = _status.asStateFlow()
    private val _saved = MutableStateFlow<Set<String>>(emptySet())
    val saved: StateFlow<Set<String>> = _saved.asStateFlow()

    @Synchronized
    private fun database(context: Context): StandaloneDatabaseProvider =
        databaseInstance ?: StandaloneDatabaseProvider(context.applicationContext).also { databaseInstance = it }

    /** The one store for song audio. Kept in the app's own storage, which Android doesn't clear. */
    @Synchronized
    fun cache(context: Context): SimpleCache {
        cacheInstance?.let { return it }
        val app = context.applicationContext
        appContext = app
        val provider = database(app)
        val old = File(app.cacheDir, "audio")  // where 0.7.0 kept played songs
        if (old.exists()) runCatching { SimpleCache.delete(old, provider) }
        return SimpleCache(File(app.filesDir, "songs"), NoOpCacheEvictor(), provider).also { cacheInstance = it }
    }

    /** Where audio comes from: a song's YouTube link turns into its audio stream when needed. */
    fun upstream(): DataSource.Factory =
        ResolvingDataSource.Factory(OkHttpDataSource.Factory(YouTube.http)) { spec -> resolve(spec) }

    private fun resolve(spec: DataSpec): DataSpec {
        val uri = spec.uri
        val videoId = uri.getQueryParameter("v")
        if (uri.host != "www.youtube.com" || videoId == null) return spec
        computerLink(videoId)?.let { return spec.withUri(Uri.parse(it)) }
        val url = YouTube.audioUrl(videoId)
        return spec.withUri(Uri.parse(url)).withAdditionalHeaders(YouTube.streamHeaders(url))
    }

    /**
     * Tells the computer which of these songs the phone still needs, and remembers the ones it
     * can send. [link] is the QR code's address. Network: call on a background thread.
     * Returns how many songs will come from the computer.
     */
    fun offerComputer(link: String, songIds: Collection<String>): Int {
        val uri = Uri.parse(link)
        val segments = uri.pathSegments
        if (segments.size != 2 || segments[0] != "t") return 0
        val base = "${uri.scheme}://${uri.encodedAuthority}"
        val token = segments[1]
        val have = _saved.value
        val need = songIds.filter { it !in have }.distinct()
        if (need.isEmpty()) return 0
        val request = okhttp3.Request.Builder()
            .url("$base/s/$token")
            .post(need.joinToString("\n").toRequestBody("text/plain".toMediaType()))
            .build()
        val offered = quick.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return 0
            response.body?.string().orEmpty().lines().map { it.trim() }.filter { it.isNotEmpty() }.toSet()
        }
        if (offered.isEmpty()) return 0
        computerFailed.clear()
        computerCheckedAt = System.currentTimeMillis()
        computer = Computer(base, token, offered)
        return offered.size
    }

    /** The computer's address for a song, if it has it and its window is still open. */
    private fun computerLink(videoId: String): String? {
        val source = computer ?: return null
        if (videoId !in source.songs || videoId in computerFailed) return null
        val link = "${source.base}/a/${source.token}/$videoId"
        if (System.currentTimeMillis() - computerCheckedAt > 10_000) {  // still there? a quick question
            val reachable = try {
                quick.newCall(okhttp3.Request.Builder().url(link).head().build()).execute().use { it.isSuccessful }
            } catch (e: IOException) {
                false
            }
            if (!reachable) {
                computer = null
                return null
            }
            computerCheckedAt = System.currentTimeMillis()
        }
        fromComputer.add(videoId)
        return link
    }

    /** Starts saving after an import, from the computer where it can. Any thread. */
    fun syncSoon(store: LibraryStore) {
        main.post { appContext?.let { sync(it, store.categories.value, retryFailed = true) } }
    }

    @Synchronized
    fun preferences(context: Context): SharedPreferences = preferencesInstance
        ?: context.applicationContext.getSharedPreferences("offline", Context.MODE_PRIVATE)
            .also { preferencesInstance = it }

    fun savingEnabled(context: Context): Boolean = preferences(context).getBoolean("save", true)

    fun mobileDataAllowed(context: Context): Boolean = preferences(context).getBoolean("mobile", false)

    private fun savingRules(context: Context) =
        Requirements(if (mobileDataAllowed(context)) Requirements.NETWORK else Requirements.NETWORK_UNMETERED)

    /** The download manager. Main thread only. */
    fun manager(context: Context): DownloadManager {
        managerInstance?.let { return it }
        val app = context.applicationContext
        val created = DownloadManager(app, database(app), cache(app), upstream(), Executors.newFixedThreadPool(2))
        created.maxParallelDownloads = 2  // gentle on YouTube
        created.requirements = savingRules(app)
        created.addListener(object : DownloadManager.Listener {
            override fun onInitialized(downloadManager: DownloadManager) = refresh()

            override fun onDownloadChanged(
                downloadManager: DownloadManager,
                download: Download,
                finalException: Exception?,
            ) {
                val id = download.request.id
                if (download.state == Download.STATE_FAILED && fromComputer.remove(id)) {
                    // The computer stopped sending (its window closed?): get this one from YouTube.
                    computerFailed.add(id)
                    send { DownloadService.sendAddDownload(app, SongDownloadService::class.java, request(id), false) }
                } else if (download.state == Download.STATE_COMPLETED) {
                    fromComputer.remove(id)
                }
                refresh()
            }

            override fun onDownloadRemoved(downloadManager: DownloadManager, download: Download) = refresh()

            override fun onIdle(downloadManager: DownloadManager) = refresh()

            override fun onRequirementsStateChanged(
                downloadManager: DownloadManager,
                requirements: Requirements,
                notMetRequirements: Int,
            ) = refresh()
        })
        managerInstance = created
        return created
    }

    /**
     * Saves every song in the library and forgets ones that were removed. Main thread.
     * Songs that failed to save are tried again on the first sync after the app starts.
     */
    fun sync(context: Context, categories: List<Category>, retryFailed: Boolean = false) {
        val app = context.applicationContext
        val ids = categories.flatMapTo(HashSet()) { category -> category.songs.map { it.videoId } }
        wanted = ids
        val manager = manager(app)
        val store = cache(app)
        val enabled = savingEnabled(app)
        val retry = retryFailed || firstSync
        firstSync = false
        io.execute {
            val known = HashMap<String, Int>()
            runCatching {
                manager.downloadIndex.getDownloads().use { cursor ->
                    while (cursor.moveToNext()) {
                        val download = cursor.download
                        known[download.request.id] = download.state
                    }
                }
            }
            // Played-but-not-saved songs live in the store too: drop the ones no longer in the library.
            for (key in store.keys.toList()) {
                val id = Uri.parse(key).getQueryParameter("v")
                if (id != null && id !in ids && id !in known) runCatching { store.removeResource(key) }
            }
            main.post {
                for (id in known.keys) {
                    if (id !in ids) send { DownloadService.sendRemoveDownload(app, SongDownloadService::class.java, id, false) }
                }
                if (enabled) {
                    for (id in ids) {
                        val state = known[id]
                        if (state == null || (retry && state == Download.STATE_FAILED)) {
                            send { DownloadService.sendAddDownload(app, SongDownloadService::class.java, request(id), false) }
                        }
                    }
                } else {
                    send { DownloadService.sendPauseDownloads(app, SongDownloadService::class.java, false) }
                }
                refresh()
            }
        }
    }

    /** About once a second while saving: measures the speed, then refreshes. Main thread. */
    fun tick() {
        val manager = managerInstance ?: return
        val now = SystemClock.elapsedRealtime()
        var received = 0L
        val seen = HashSet<String>()
        for (download in manager.currentDownloads) {
            val id = download.request.id
            seen.add(id)
            val bytes = download.bytesDownloaded
            val before = lastBytes[id]
            if (before != null && bytes > before) received += bytes - before
            lastBytes[id] = bytes
        }
        lastBytes.keys.retainAll(seen)
        val elapsed = now - lastSampleAt
        if (lastSampleAt > 0L && elapsed in 1L..10_000L) {
            val instant = received * 1000.0 / elapsed
            bytesPerSecond = if (bytesPerSecond == 0.0) instant else bytesPerSecond * 0.5 + instant * 0.5
        }
        lastSampleAt = now
        if (seen.isEmpty()) bytesPerSecond = 0.0
        refresh()
    }

    /** Recomputes what the progress bar shows. Main thread (it reads live download progress). */
    fun refresh() {
        val manager = managerInstance ?: return
        val store = cacheInstance ?: return
        val notMet = manager.notMetRequirements
        val live = manager.currentDownloads.associateBy { it.request.id }
        val enabled = preferencesInstance?.getBoolean("save", true) ?: true
        val ids = wanted
        val speed = if (live.isEmpty()) 0.0 else bytesPerSecond
        val copying = computer != null && fromComputer.isNotEmpty()
        io.execute {
            val all = HashMap<String, Download>()
            runCatching {
                manager.downloadIndex.getDownloads().use { cursor ->
                    while (cursor.moveToNext()) {
                        val download = cursor.download
                        all[download.request.id] = download
                    }
                }
            }
            all.putAll(live)  // the index only updates on state changes; these have live progress
            var done = 0
            var failed = 0
            var partial = 0f
            val savedIds = HashSet<String>()
            for (id in ids) {
                val download = all[id] ?: continue
                when (download.state) {
                    Download.STATE_COMPLETED -> {
                        done += 1
                        savedIds.add(id)
                    }
                    Download.STATE_FAILED -> failed += 1
                    Download.STATE_DOWNLOADING -> partial += download.percentDownloaded.coerceAtLeast(0f) / 100f
                }
            }
            _saved.value = savedIds
            _status.value = SaveStatus(
                saved = done,
                total = ids.size,
                progress = if (ids.isEmpty()) 0f else ((done + partial) / ids.size).coerceIn(0f, 1f),
                failed = failed,
                bytes = store.cacheSpace,
                waitingForWifi = (notMet and Requirements.NETWORK_UNMETERED) != 0,
                waitingForNetwork = (notMet and Requirements.NETWORK) != 0,
                enabled = enabled,
                active = live.isNotEmpty(),
                bytesPerSecond = speed,
                fromComputer = copying,
            )
        }
    }

    fun setSaving(context: Context, on: Boolean, categories: List<Category>) {
        val app = context.applicationContext
        preferences(app).edit().putBoolean("save", on).apply()
        if (on) {
            send { DownloadService.sendResumeDownloads(app, SongDownloadService::class.java, false) }
            sync(app, categories)
        } else {
            send { DownloadService.sendPauseDownloads(app, SongDownloadService::class.java, false) }
            refresh()
        }
    }

    fun setMobileData(context: Context, allowed: Boolean) {
        val app = context.applicationContext
        preferences(app).edit().putBoolean("mobile", allowed).apply()
        send { DownloadService.sendSetRequirements(app, SongDownloadService::class.java, savingRules(app), false) }
    }

    /** Frees the space: removes every saved song, and stops saving until it's turned back on. */
    fun deleteSaved(context: Context) {
        val app = context.applicationContext
        preferences(app).edit().putBoolean("save", false).apply()
        send { DownloadService.sendRemoveAllDownloads(app, SongDownloadService::class.java, false) }
        val store = cache(app)
        io.execute {
            for (key in store.keys.toList()) runCatching { store.removeResource(key) }
            main.post { refresh() }
        }
    }

    private fun request(videoId: String): DownloadRequest {
        val link = "https://www.youtube.com/watch?v=$videoId"
        return DownloadRequest.Builder(videoId, Uri.parse(link)).setCustomCacheKey(link).build()
    }

    private fun send(action: () -> Unit) {
        try {
            action()
        } catch (e: IllegalStateException) {
            // Android won't start services from the background; the next sync catches up.
        }
    }
}

private const val SAVING_NOTIFICATION = 2
private const val SAVING_CHANNEL = "saving"

/** Keeps saving going with the screen off, showing progress in a notification. */
@OptIn(UnstableApi::class)
class SongDownloadService : DownloadService(
    SAVING_NOTIFICATION,
    DownloadService.DEFAULT_FOREGROUND_NOTIFICATION_UPDATE_INTERVAL,
    SAVING_CHANNEL,
    R.string.saving_channel,
    0,
) {
    override fun getDownloadManager(): DownloadManager = Offline.manager(this)

    override fun getScheduler(): Scheduler? = null

    override fun getForegroundNotification(downloads: MutableList<Download>, notMetRequirements: Int): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return DownloadNotificationHelper(this, SAVING_CHANNEL).buildProgressNotification(
            this, R.drawable.ic_saving, open, "Saving songs for offline", downloads, notMetRequirements,
        )
    }
}
