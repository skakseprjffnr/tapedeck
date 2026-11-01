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
    val downloading: Int = 0,
    val queued: Int = 0,
    val computerSpeed: Double = 0.0,
    val youtubeSpeed: Double = 0.0,
    val computerConnected: Boolean = false,
    val ageRestricted: Int = 0,
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
    private const val ORIGIN_COMPUTER = "pc"  // YouTube origins are "yt:<format>"
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

    @Volatile
    private var computerRetryAt = 0L

    /** The last few songs that couldn't be saved, and why (for Settings' saving details). */
    private val problems = ArrayDeque<String>()

    // Where each partly stored song came from. The rest must come from the same place, or the
    // saved file would be two different files glued together. Kept across restarts.
    private val origins = ConcurrentHashMap<String, String>()
    private val startingOver: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private val restarts = ConcurrentHashMap<String, Int>()
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
    private var computerSpeed = 0.0
    private var youtubeSpeed = 0.0
    private var finishedFromComputer = 0L  // bytes of songs that finished between two samples
    private var finishedFromYouTube = 0L

    @Volatile
    private var wanted: Set<String> = emptySet()

    private val _status = MutableStateFlow(SaveStatus())
    val status: StateFlow<SaveStatus> = _status.asStateFlow()
    private val _saved = MutableStateFlow<Set<String>>(emptySet())
    val saved: StateFlow<Set<String>> = _saved.asStateFlow()

    /** Songs YouTube plays only for a signed-in account: they can only come from the computer. */
    private val _ageRestricted = MutableStateFlow<Set<String>>(emptySet())
    val ageRestricted: StateFlow<Set<String>> = _ageRestricted.asStateFlow()

    /** Songs saved by hand: kept, and saved even with automatic saving off. */
    private val _pinned = MutableStateFlow<Set<String>>(emptySet())
    val pinned: StateFlow<Set<String>> = _pinned.asStateFlow()

    private fun setPinned(next: Set<String>) {
        _pinned.value = next
        preferencesInstance?.edit()?.putStringSet("pinned", HashSet(next))?.apply()
    }

    /** Save these songs now, and keep them even with automatic saving off. Main thread. */
    fun pin(context: Context, songIds: Collection<String>, categories: List<Category>) {
        val app = context.applicationContext
        preferences(app)
        setPinned(_pinned.value + songIds)
        sync(app, categories, retryFailed = true)
    }

    /** Stop keeping these; with automatic saving off, their saved copies are deleted. */
    fun unpin(context: Context, songIds: Collection<String>, categories: List<Category>) {
        val app = context.applicationContext
        preferences(app)
        setPinned(_pinned.value - songIds.toSet())
        if (!savingEnabled(app)) {
            for (id in songIds) {
                clearOrigin(id)
                send { DownloadService.sendRemoveDownload(app, SongDownloadService::class.java, id, false) }
            }
        }
        sync(app, categories)
    }

    fun audioFormat(context: Context): String = preferences(context).getString("format", "auto") ?: "auto"

    fun audioQuality(context: Context): String = preferences(context).getString("quality", "best") ?: "best"

    /** The format and quality for songs saved from YouTube from now on. */
    fun setAudio(context: Context, format: String, quality: String) {
        preferences(context).edit().putString("format", format).putString("quality", quality).apply()
        YouTube.preferredFormat = format
        YouTube.preferredQuality = quality
        YouTube.forgetAll()  // lookups made with the old choice
    }

    private fun markAgeRestricted(id: String, restricted: Boolean) {
        val now = _ageRestricted.value
        if ((id in now) == restricted) return
        val next = if (restricted) now + id else now - id
        _ageRestricted.value = next
        preferencesInstance?.edit()?.putStringSet("age_restricted", HashSet(next))?.apply()
    }

    /** Where saved songs are kept: the app's own storage, which only Tapedeck can open. */
    fun folderPath(context: Context): String = File(context.applicationContext.filesDir, "songs").absolutePath

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
    fun upstream(): DataSource.Factory {
        val network = OkHttpDataSource.Factory(YouTube.http)
        // YouTube streams are read in bounded pieces, which YouTube serves at full speed.
        val pieces = DataSource.Factory { ChunkedDataSource(network.createDataSource()) }
        return ResolvingDataSource.Factory(pieces) { spec -> resolve(spec) }
    }

    private fun resolve(spec: DataSpec): DataSpec {
        val uri = spec.uri
        val videoId = uri.getQueryParameter("v")
        if (uri.host != "www.youtube.com" || videoId == null) return spec
        // Part of the song may be stored already: the rest has to come from the same place.
        val resuming = spec.position > 0
        val before = if (resuming) originOf(videoId) else null
        if (resuming && before == null) startOver(videoId)  // stored by an older version, source unknown
        if (before == null || before == ORIGIN_COMPUTER) {
            computerLink(videoId)?.let { link ->
                if (!resuming) setOrigin(videoId, ORIGIN_COMPUTER)
                return spec.withUri(Uri.parse(link))
            }
            if (before == ORIGIN_COMPUTER) startOver(videoId)  // the computer isn't offering it any more
        }
        val url = try {
            YouTube.audioUrl(videoId, before?.removePrefix("yt:"))
        } catch (e: YouTube.FormatGone) {
            startOver(videoId)
        }
        if (!resuming) setOrigin(videoId, "yt:" + (YouTube.itagOf(url) ?: "?"))
        return spec.withUri(Uri.parse(url)).withAdditionalHeaders(YouTube.streamHeaders(url))
    }

    private fun originOf(id: String): String? =
        origins[id] ?: preferencesInstance?.getString("origin_$id", null)?.also { origins[id] = it }

    private fun setOrigin(id: String, origin: String) {
        origins[id] = origin
        preferencesInstance?.edit()?.putString("origin_$id", origin)?.apply()
    }

    private fun clearOrigin(id: String) {
        origins.remove(id)
        preferencesInstance?.edit()?.remove("origin_$id")?.apply()
    }

    /** The stored part came from somewhere else: fail this attempt, so it starts over cleanly. */
    private fun startOver(videoId: String): Nothing {
        fromComputer.remove(videoId)
        startingOver.add(videoId)
        throw IOException("Starting this song over, from a different source")
    }

    /** Throws away a song's partly saved copy and saves it again from the start. Main thread. */
    private fun saveAgainFromStart(app: Context, id: String) {
        val attempts = (restarts[id] ?: 0) + 1
        restarts[id] = attempts
        if (attempts > 3) return  // keeps failing: leave it failed (Try again restarts it)
        clearOrigin(id)
        val store = cacheInstance ?: return
        io.execute {
            runCatching { store.removeResource(keyOf(id)) }
            main.post { send { DownloadService.sendAddDownload(app, SongDownloadService::class.java, request(id), false) } }
        }
    }

    private fun keyOf(id: String) = "https://www.youtube.com/watch?v=$id"

    /**
     * After a scan: songs partly saved from YouTube start over from the computer, which sends
     * them far faster. Songs that haven't started pick the computer by themselves. Any thread.
     */
    fun switchToComputer() {
        val source = computer ?: return
        main.post {
            val app = appContext ?: return@post
            val manager = managerInstance ?: return@post
            for (download in manager.currentDownloads) {
                val id = download.request.id
                if (id in source.songs && originOf(id)?.startsWith("yt:") == true) {
                    restarts.remove(id)
                    clearOrigin(id)
                    send { DownloadService.sendRemoveDownload(app, SongDownloadService::class.java, id, false) }
                    send { DownloadService.sendAddDownload(app, SongDownloadService::class.java, request(id), false) }
                }
            }
        }
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
        computerRetryAt = 0L
        computer = Computer(base, token, offered)
        return offered.size
    }

    /** The computer's address for a song, if it has it and its window is still open. */
    private fun computerLink(videoId: String): String? {
        val source = computer ?: return null
        if (videoId !in source.songs || videoId in computerFailed) return null
        val link = "${source.base}/a/${source.token}/$videoId"
        val now = System.currentTimeMillis()
        if (now < computerRetryAt) return null  // it didn't answer a moment ago: YouTube for now
        if (now - computerCheckedAt > 10_000) {  // still there? a quick question
            val reachable = try {
                quick.newCall(okhttp3.Request.Builder().url(link).head().build()).execute().use { it.isSuccessful }
            } catch (e: IOException) {
                false
            }
            if (!reachable) {
                // Ask again shortly: one missed answer on busy Wi-Fi shouldn't send every
                // remaining song to YouTube.
                computerRetryAt = now + 20_000
                return null
            }
            computerCheckedAt = now
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
        ?: context.applicationContext.getSharedPreferences("offline", Context.MODE_PRIVATE).also {
            preferencesInstance = it
            _ageRestricted.value = it.getStringSet("age_restricted", emptySet())?.toSet() ?: emptySet()
            _pinned.value = it.getStringSet("pinned", emptySet())?.toSet() ?: emptySet()
            YouTube.preferredFormat = it.getString("format", "auto") ?: "auto"
            YouTube.preferredQuality = it.getString("quality", "best") ?: "best"
        }

    fun savingEnabled(context: Context): Boolean = preferences(context).getBoolean("save", true)

    fun mobileDataAllowed(context: Context): Boolean = preferences(context).getBoolean("mobile", false)

    private fun savingRules(context: Context) =
        Requirements(if (mobileDataAllowed(context)) Requirements.NETWORK else Requirements.NETWORK_UNMETERED)

    /** The download manager. Main thread only. */
    fun manager(context: Context): DownloadManager {
        managerInstance?.let { return it }
        val app = context.applicationContext
        val created = DownloadManager(app, database(app), cache(app), upstream(), Executors.newFixedThreadPool(2))
        created.maxParallelDownloads = 3  // quick, and still gentle on YouTube
        created.requirements = savingRules(app)
        created.addListener(object : DownloadManager.Listener {
            override fun onInitialized(downloadManager: DownloadManager) = refresh()

            override fun onDownloadChanged(
                downloadManager: DownloadManager,
                download: Download,
                finalException: Exception?,
            ) {
                val id = download.request.id
                if (download.state == Download.STATE_FAILED && YouTube.isAgeRestricted(finalException)) {
                    // Only a signed-in account can get it from YouTube; the computer can send it.
                    fromComputer.remove(id)
                    startingOver.remove(id)
                    markAgeRestricted(id, true)
                } else if (download.state == Download.STATE_FAILED) {
                    val switching = startingOver.remove(id)
                    val computerQuit = fromComputer.remove(id)
                    // The computer stopped sending (its window closed?): YouTube for this one now.
                    if (computerQuit && !switching) computerFailed.add(id)
                    if (switching || computerQuit) {
                        saveAgainFromStart(app, id)  // from the start, so the two sources never mix
                    } else {
                        problems.addLast("$id: ${finalException?.message ?: "failed"}")
                        while (problems.size > 8) problems.removeFirst()
                    }
                } else if (download.state == Download.STATE_COMPLETED) {
                    // Count the part not seen by a speed sample yet (fast songs finish in between).
                    val counted = lastBytes.remove(id) ?: 0L
                    val rest = (download.bytesDownloaded - counted).coerceAtLeast(0L)
                    if (originOf(id) == ORIGIN_COMPUTER) finishedFromComputer += rest else finishedFromYouTube += rest
                    fromComputer.remove(id)
                    restarts.remove(id)
                    clearOrigin(id)
                    markAgeRestricted(id, false)  // it arrived (from the computer)
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
        val library = categories.flatMapTo(HashSet()) { category -> category.songs.map { it.videoId } }
        val manager = manager(app)
        val store = cache(app)
        val enabled = savingEnabled(app)
        // Automatic saving keeps every song; otherwise only the ones saved by hand.
        val ids = if (enabled) library else library.intersect(_pinned.value)
        wanted = ids
        val retry = retryFailed || firstSync
        firstSync = false
        if (retry) restarts.clear()  // songs that gave up get their fresh starts back
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
                if (id != null && id !in library && id !in known) runCatching { store.removeResource(key) }
            }
            main.post {
                for ((id, state) in known) {
                    // Gone from the library, or (with automatic saving off) not saved by hand and
                    // not finished: stop it. Songs already saved stay.
                    val unwanted = id !in ids && state != Download.STATE_COMPLETED
                    if (id !in library || unwanted) {
                        send { DownloadService.sendRemoveDownload(app, SongDownloadService::class.java, id, false) }
                    }
                }
                run {
                    val restricted = _ageRestricted.value
                    val offered = computer?.songs ?: emptySet()
                    for (id in ids) {
                        // YouTube won't give the phone an age-restricted song; only the computer can.
                        if (id in restricted && id !in offered) continue
                        val state = known[id]
                        if (state == null || (retry && state == Download.STATE_FAILED)) {
                            send { DownloadService.sendAddDownload(app, SongDownloadService::class.java, request(id), false) }
                        }
                    }
                    // The download manager starts out paused, and only the saving service resumes
                    // it. Songs queued before the app restarted wait for that, so start it now.
                    send { DownloadService.sendResumeDownloads(app, SongDownloadService::class.java, false) }
                }
                refresh()
            }
        }
    }

    /** About once a second while saving: measures the speed, then refreshes. Main thread. */
    fun tick() {
        val manager = managerInstance ?: return
        val now = SystemClock.elapsedRealtime()
        var fromPc = finishedFromComputer
        var fromYt = finishedFromYouTube
        finishedFromComputer = 0L
        finishedFromYouTube = 0L
        val seen = HashSet<String>()
        for (download in manager.currentDownloads) {
            val id = download.request.id
            seen.add(id)
            val bytes = download.bytesDownloaded
            val before = lastBytes[id]
            if (before != null && bytes > before) {
                if (originOf(id) == ORIGIN_COMPUTER) fromPc += bytes - before else fromYt += bytes - before
            }
            lastBytes[id] = bytes
        }
        lastBytes.keys.retainAll(seen)
        val elapsed = now - lastSampleAt
        if (lastSampleAt > 0L && elapsed in 1L..10_000L) {
            computerSpeed = smooth(computerSpeed, fromPc * 1000.0 / elapsed)
            youtubeSpeed = smooth(youtubeSpeed, fromYt * 1000.0 / elapsed)
        }
        lastSampleAt = now
        if (seen.isEmpty() && fromPc == 0L && fromYt == 0L) {
            computerSpeed = 0.0
            youtubeSpeed = 0.0
        }
        bytesPerSecond = computerSpeed + youtubeSpeed
        refresh()
    }

    private fun smooth(previous: Double, latest: Double): Double =
        if (previous == 0.0) latest else previous * 0.5 + latest * 0.5

    /** Recomputes what the progress bar shows. Main thread (it reads live download progress). */
    fun refresh() {
        val manager = managerInstance ?: return
        val store = cacheInstance ?: return
        val notMet = manager.notMetRequirements
        val live = manager.currentDownloads.associateBy { it.request.id }
        val enabled = preferencesInstance?.getBoolean("save", true) ?: true
        val ids = wanted
        val speed = bytesPerSecond
        val copying = computer != null && fromComputer.isNotEmpty()
        val fromPcSpeed = computerSpeed
        val fromYtSpeed = youtubeSpeed
        val connected = computer != null
        if (manager.downloadsPaused) {  // nothing should stay paused: songs saved by hand run too
            appContext?.let { app -> send { DownloadService.sendResumeDownloads(app, SongDownloadService::class.java, false) } }
        }
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
            var restricted = 0
            val restrictedIds = _ageRestricted.value
            var downloading = 0
            var queued = 0
            var partial = 0f
            val savedIds = HashSet<String>()
            for (id in ids) {
                val download = all[id] ?: continue
                when (download.state) {
                    Download.STATE_COMPLETED -> {
                        done += 1
                        savedIds.add(id)
                    }
                    Download.STATE_FAILED -> if (id in restrictedIds) restricted += 1 else failed += 1
                    Download.STATE_DOWNLOADING -> {
                        downloading += 1
                        partial += download.percentDownloaded.coerceAtLeast(0f) / 100f
                    }
                    Download.STATE_QUEUED -> queued += 1
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
                downloading = downloading,
                queued = queued,
                computerSpeed = fromPcSpeed,
                youtubeSpeed = fromYtSpeed,
                computerConnected = connected,
                ageRestricted = restricted,
            )
        }
    }

    /** Where saving stands, as text to copy and paste when something seems stuck. Main thread. */
    fun report(notificationsOn: Boolean): String {
        val state = _status.value
        val manager = managerInstance
        val notMet = manager?.notMetRequirements ?: 0
        val source = computer
        val waitingFor = when {
            (notMet and Requirements.NETWORK) != 0 -> "an internet connection"
            (notMet and Requirements.NETWORK_UNMETERED) != 0 -> "Wi-Fi (this network counts as mobile data)"
            notMet != 0 -> "something else ($notMet)"
            else -> "nothing"
        }
        return buildString {
            appendLine("Tapedeck saving details")
            appendLine("Songs: ${state.total}; saved ${state.saved}, saving ${state.downloading}, " +
                "waiting ${state.queued}, failed ${state.failed}, age-restricted ${state.ageRestricted}")
            appendLine("Saving: ${if (state.enabled) "on" else "off"}; mobile data: " +
                "${if (preferencesInstance?.getBoolean("mobile", false) == true) "allowed" else "not allowed"}; " +
                "paused: ${if (manager?.downloadsPaused == true) "yes" else "no"}")
            appendLine("Waiting for: $waitingFor")
            appendLine("Notifications: ${if (notificationsOn) "on" else "off"}")
            appendLine("Computer: " + if (source == null) "not connected" else
                "${source.base} (${source.songs.size} songs offered, ${fromComputer.size} coming now)")
            appendLine("Speed: from the computer ${formatSpeed(state.computerSpeed)}, from YouTube " +
                "${formatSpeed(state.youtubeSpeed)}; stored: ${formatBytes(state.bytes)}")
            if (problems.isNotEmpty()) {
                appendLine("Recent problems:")
                for (problem in problems) appendLine("  $problem")
            }
        }.trimEnd()
    }

    fun setSaving(context: Context, on: Boolean, categories: List<Category>) {
        val app = context.applicationContext
        preferences(app).edit().putBoolean("save", on).apply()
        sync(app, categories)  // off: songs saved by hand carry on; unfinished others stop
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
        setPinned(emptySet())
        send { DownloadService.sendRemoveAllDownloads(app, SongDownloadService::class.java, false) }
        val store = cache(app)
        io.execute {
            for (key in store.keys.toList()) runCatching { store.removeResource(key) }
            main.post { refresh() }
        }
    }

    private fun request(videoId: String): DownloadRequest {
        val link = keyOf(videoId)
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
