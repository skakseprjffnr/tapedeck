package app.tapedeck.player

import android.app.PendingIntent
import android.content.Intent
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/**
 * Plays in the background, with the lock screen and notification controls Android provides.
 *
 * Each song's address is its YouTube watch link. Songs are read from the phone's store first
 * (see Offline), so saved ones start instantly and work offline; only for the rest is the
 * real audio stream looked up.
 */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private var failures = 0

    override fun onCreate() {
        super.onCreate()
        val cached = CacheDataSource.Factory()
            .setCache(Offline.cache(this))
            .setUpstreamDataSourceFactory(Offline.upstream())
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        // The player's defaults are made for video: keep under a minute ahead, throw away what
        // has played, and wait for 2.5 s of audio after every jump. So each jump reloaded the
        // song, from YouTube or the computer when that part wasn't stored, which was the pause.
        // A song is only a few MB: keep all of it loaded, played part included (up to 32 MB),
        // so jumping anywhere in it is instant, and resume after half a second otherwise.
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(
                600_000,  // keep loading until 10 minutes ahead are loaded...
                600_000,  // ...which covers nearly every song entirely
                500,      // after a jump outside what's loaded: half a second of audio
                1_000,    // after running dry mid-song: a second
            )
            .setBackBuffer(600_000, true)  // keep what's been played, so rewinding is instant too
            .setTargetBufferBytes(32 * 1024 * 1024)
            .setPrioritizeTimeOverSizeThresholds(false)  // the 32 MB cap wins over the 10 minutes
            .build()
        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cached))
            .setLoadControl(loadControl)
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
}
