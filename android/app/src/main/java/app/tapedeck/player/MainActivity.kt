package app.tapedeck.player

import android.content.ComponentName
import android.content.Context
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import androidx.core.content.ContextCompat
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private var pending: ListenableFuture<MediaController>? = null
    private val controller = mutableStateOf<MediaController?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(  // light bar icons: the app is always dark
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        val store = Tapedeck.store(this)
        Tapedeck.fillInDetails(store)
        setContent { TapedeckApp(store, controller.value) }
    }

    override fun onStart() {
        super.onStart()
        val token = SessionToken(this, ComponentName(this, PlaybackService::class.java))
        val future = MediaController.Builder(this, token).buildAsync()
        pending = future
        future.addListener({
            controller.value = try {
                future.get()
            } catch (e: Exception) {
                null  // released before it connected
            }
        }, ContextCompat.getMainExecutor(this))
    }

    override fun onStop() {
        // Playback carries on in the service; the screen just lets go of its remote control.
        controller.value = null
        pending?.let { MediaController.releaseFuture(it) }
        pending = null
        super.onStop()
    }
}

/** The library, shared by the screen and background work. */
object Tapedeck {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val filling = AtomicBoolean(false)

    @Volatile
    private var library: LibraryStore? = null

    fun store(context: Context): LibraryStore = library ?: synchronized(this) {
        library ?: LibraryStore(context.applicationContext).also { library = it }
    }

    /** Looks up titles for songs that arrived without one, a few at a time. */
    fun fillInDetails(store: LibraryStore) {
        if (!filling.compareAndSet(false, true)) return
        scope.launch {
            try {
                for (batch in store.missingDetails().chunked(10)) {
                    val found = HashMap<String, Pair<String, String>>()
                    for (videoId in batch) {
                        YouTube.details(videoId)?.let { found[videoId] = it }
                    }
                    store.fillIn(found)
                }
            } finally {
                filling.set(false)
            }
        }
    }
}
