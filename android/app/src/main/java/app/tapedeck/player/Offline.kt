package app.tapedeck.player

import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.os.Handler
import android.os.Looper
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
import java.util.concurrent.Executors
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

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
)

/**
 * Saves every song in the library onto the phone, so it plays offline.
 *
 * Saved songs and played songs share one store, keyed by each song's YouTube link. The player
 * reads from it first, so a saved song never touches the network. Saving runs in a foreground
 * service with a progress notification, on Wi-Fi unless mobile data is allowed, and picks up
 * where it left off after the app or the phone restarts.
 */
@OptIn(UnstableApi::class)
object Offline {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var databaseInstance: StandaloneDatabaseProvider? = null
    private var cacheInstance: SimpleCache? = null
    private var managerInstance: DownloadManager? = null
    private var preferencesInstance: SharedPreferences? = null
    private var firstSync = true

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
        val url = YouTube.audioUrl(videoId)
        return spec.withUri(Uri.parse(url)).withAdditionalHeaders(YouTube.streamHeaders(url))
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
            ) = refresh()

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

    /** Recomputes what the progress bar shows. Main thread (it reads live download progress). */
    fun refresh() {
        val manager = managerInstance ?: return
        val store = cacheInstance ?: return
        val notMet = manager.notMetRequirements
        val live = manager.currentDownloads.associateBy { it.request.id }
        val enabled = preferencesInstance?.getBoolean("save", true) ?: true
        val ids = wanted
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
