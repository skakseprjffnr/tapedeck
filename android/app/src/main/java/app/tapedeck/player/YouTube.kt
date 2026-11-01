package app.tapedeck.player

import android.net.Uri
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.schabi.newpipe.extractor.NewPipe
import org.schabi.newpipe.extractor.downloader.Downloader
import org.schabi.newpipe.extractor.downloader.Request
import org.schabi.newpipe.extractor.downloader.Response
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException
import org.schabi.newpipe.extractor.stream.DeliveryMethod
import org.schabi.newpipe.extractor.stream.StreamInfo

/** YouTube through NewPipeExtractor, which solves YouTube's JavaScript checks in-app. */
object YouTube {
    private const val BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
    private const val STREAM_LIFETIME_MS = 45 * 60 * 1000L  // YouTube's stream links expire after a few hours

    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private class Found(val url: String, val at: Long)

    private val found = ConcurrentHashMap<String, Found>()
    private var started = false

    @Synchronized
    private fun start() {
        if (!started) {
            NewPipe.init(Fetcher)
            started = true
        }
    }

    /** YouTube plays this only for a signed-in, age-verified account; the phone can't sign in. */
    class AgeRestricted : IOException(
        "Age-restricted: YouTube plays it only for a signed-in account. It can come from your " +
            "computer, signed in there.",
    )

    private val AGE_WORDS = Regex("age[- ]restrict|confirm your age|inappropriate for some users",
        RegexOption.IGNORE_CASE)

    /** Whether an error (or what caused it) is YouTube's age restriction. */
    fun isAgeRestricted(error: Throwable?): Boolean =
        generateSequence(error) { it.cause }.take(8).any { cause ->
            cause is AgeRestricted || cause.javaClass.simpleName == "AgeRestrictedContentException" ||
                cause.message?.let { AGE_WORDS.containsMatchIn(it) } == true
        }

    /** YouTube no longer offers the format a partly saved copy began in, so it can't be finished. */
    class FormatGone(message: String) : IOException(message)

    fun itagOf(url: String): String? = Uri.parse(url).getQueryParameter("itag")

    /**
     * The best audio-only stream for a song, on a background thread (the player's loader).
     * With [itag], exactly that format, so a partly saved copy is finished with the same bytes.
     */
    fun audioUrl(videoId: String, itag: String? = null): String {
        found[videoId]?.let {
            val fresh = System.currentTimeMillis() - it.at < STREAM_LIFETIME_MS
            if (fresh && (itag == null || itagOf(it.url) == itag)) return it.url
        }
        start()
        val info = try {
            StreamInfo.getInfo("https://www.youtube.com/watch?v=$videoId")
        } catch (e: Exception) {
            if (isAgeRestricted(e)) throw AgeRestricted()
            if (e is IOException) throw e
            throw IOException(e.message ?: "YouTube wouldn't play this song", e)
        }
        val streams = info.audioStreams.filter { it.deliveryMethod == DeliveryMethod.PROGRESSIVE_HTTP && it.isUrl }
        val chosen = if (itag != null) {
            streams.firstOrNull { itagOf(it.content) == itag } ?: throw FormatGone("That format of the song is gone")
        } else {
            streams.maxByOrNull { it.averageBitrate } ?: throw IOException("YouTube didn't offer audio for this song")
        }
        found[videoId] = Found(chosen.content, System.currentTimeMillis())
        return chosen.content
    }

    /** After a playback error, look the song up fresh next time. */
    fun forget(videoId: String) {
        found.remove(videoId)
    }

    /** Streams meant for YouTube's website expect the website's headers. */
    fun streamHeaders(url: String): Map<String, String> {
        val client = Uri.parse(url).getQueryParameter("c").orEmpty()
        return if (client.startsWith("WEB") || client.startsWith("MWEB") || client.startsWith("TVHTML5")) {
            mapOf("User-Agent" to BROWSER, "Origin" to "https://www.youtube.com", "Referer" to "https://www.youtube.com/")
        } else {
            emptyMap()
        }
    }

    /** Title and channel through YouTube's oEmbed: quick, and needs no extraction. */
    fun details(videoId: String): Pair<String, String>? = try {
        val url = "https://www.youtube.com/oembed".toHttpUrl().newBuilder()
            .addQueryParameter("format", "json")
            .addQueryParameter("url", "https://www.youtube.com/watch?v=$videoId")
            .build()
        http.newCall(okhttp3.Request.Builder().url(url).build()).execute().use { response ->
            if (!response.isSuccessful) {
                null
            } else {
                val json = JSONObject(response.body?.string().orEmpty())
                json.optString("title", "").takeIf { it.isNotEmpty() }?.let { it to json.optString("author_name", "") }
            }
        }
    } catch (e: Exception) {
        null
    }

    fun fetchText(url: String): String {
        http.newCall(okhttp3.Request.Builder().url(url).build()).execute().use { response ->
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
            return response.body?.string().orEmpty()
        }
    }

    private object Fetcher : Downloader() {
        override fun execute(request: Request): Response {
            val builder = okhttp3.Request.Builder().url(request.url())
            var hasAgent = false
            for ((name, values) in request.headers()) {
                if (name.equals("User-Agent", ignoreCase = true)) hasAgent = true
                builder.removeHeader(name)
                for (value in values) builder.addHeader(name, value)
            }
            if (!hasAgent) builder.header("User-Agent", BROWSER)
            builder.method(request.httpMethod(), request.dataToSend()?.toRequestBody())
            http.newCall(builder.build()).execute().use { response ->
                if (response.code == 429) {
                    throw ReCaptchaException("YouTube is asking this phone to slow down", request.url())
                }
                return Response(response.code, response.message, response.headers.toMultimap(),
                    response.body?.string(), response.request.url.toString())
            }
        }
    }
}

/** Import from a scanned QR code (a one-time link to the computer) or a pasted code. */
object Importer {
    fun run(store: LibraryStore, input: String): String {
        val text = input.trim()
        val isLink = text.startsWith("http://", ignoreCase = true) || text.startsWith("https://", ignoreCase = true)
        val code = if (!isLink) text else try {
            YouTube.fetchText(text)
        } catch (e: IOException) {
            throw IllegalStateException("Couldn't reach your computer. Check that your phone is on the same " +
                "Wi-Fi and that Copy to your phone is still open there, then scan again.")
        }
        // Before importing (which starts saving), find out which songs the computer can send,
        // so those never start downloading from YouTube.
        var fromComputer = 0
        if (isLink) {
            Codes.read(code)?.let { parsed ->
                val songIds = parsed.categories.flatMap { category -> category.songs.map { it.videoId } }
                fromComputer = try {
                    Offline.offerComputer(text, songIds)
                } catch (e: IOException) {
                    0  // the songs come from YouTube instead
                }
            }
        }
        val result = store.importCode(code)
        Tapedeck.fillInDetails(store)
        Offline.syncSoon(store)
        if (fromComputer > 0) Offline.switchToComputer()  // songs partway through from YouTube
        val parts = ArrayList<String>()
        if (result.added > 0) parts.add("added ${result.added} ${if (result.added == 1) "category" else "categories"}")
        if (result.updated > 0) parts.add("updated ${result.updated}")
        val copying = if (fromComputer > 0) " Copying ${songCount(fromComputer)} from your computer." else ""
        return "Imported: ${parts.joinToString(" and ")}.$copying"
    }
}
