package app.tapedeck.player

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import java.io.File

/**
 * Plays in the background, with the lock screen and notification controls Android provides.
 *
 * Each song's address is its YouTube watch link. The cache is keyed by that link, so a song
 * that's been played once starts instantly (and works offline); only on a cache miss is
 * the real audio stream looked up.
 */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private var failures = 0

    override fun onCreate() {
        super.onCreate()
        val network = OkHttpDataSource.Factory(YouTube.http)
        val resolving = ResolvingDataSource.Factory(network) { spec -> resolve(spec) }
        val cached = CacheDataSource.Factory()
            .setCache(audioCache(this))
            .setUpstreamDataSourceFactory(resolving)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cached))
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .build(),
                true,  // pause for calls and other apps' audio
            )
            .setHandleAudioBecomingNoisy(true)  // pause when headphones are unplugged
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                // Skip a song YouTube won't play, but don't race through the whole queue offline.
                player.currentMediaItem?.mediaId?.let { YouTube.forget(it) }
                failures += 1
                if (failures < 5 && player.hasNextMediaItem()) {
                    player.seekToNextMediaItem()
                    player.prepare()
                    player.play()
                }
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) failures = 0
            }
        })
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, player)
            .setCallback(SessionCallback)
            .setSessionActivity(open)
            .build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        val player = session?.player
        if (player == null || !player.playWhenReady || player.mediaItemCount == 0) stopSelf()
    }

    override fun onDestroy() {
        session?.run {
            player.release()
            release()
        }
        session = null
        super.onDestroy()
    }

    private fun resolve(spec: DataSpec): DataSpec {
        val uri = spec.uri
        val videoId = uri.getQueryParameter("v")
        if (uri.host != "www.youtube.com" || videoId == null) return spec
        val url = YouTube.audioUrl(videoId)
        return spec.withUri(Uri.parse(url)).withAdditionalHeaders(YouTube.streamHeaders(url))
    }

    /** Songs arrive from the app's screen; rebuild their address from the id the screen sent. */
    private object SessionCallback : MediaSession.Callback {
        override fun onAddMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>,
        ): ListenableFuture<MutableList<MediaItem>> = Futures.immediateFuture(
            mediaItems.map { item ->
                item.buildUpon().setUri("https://www.youtube.com/watch?v=${item.mediaId}").build()
            }.toMutableList()
        )
    }

    companion object {
        @Volatile
        private var cache: SimpleCache? = null

        /** Up to 3 GB of recently played songs. Android may clear it when storage runs low. */
        fun audioCache(context: Context): SimpleCache = cache ?: synchronized(this) {
            cache ?: SimpleCache(
                File(context.cacheDir, "audio"),
                LeastRecentlyUsedCacheEvictor(3L shl 30),
                StandaloneDatabaseProvider(context),
            ).also { cache = it }
        }
    }
}
