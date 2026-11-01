package app.tapedeck.player

import android.content.Context
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.Base64
import java.util.UUID
import java.util.zip.Inflater
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject

const val DEFAULT_ACCENT = "#4592a2"

data class Song(
    val videoId: String,
    val title: String,
    val channel: String,
    val seconds: Int,
) {
    val watchUrl: String get() = "https://www.youtube.com/watch?v=$videoId"
    val musicUrl: String get() = "https://music.youtube.com/watch?v=$videoId"
    val thumbnail: String get() = "https://i.ytimg.com/vi/$videoId/mqdefault.jpg"
}

data class Category(
    val id: String,
    val title: String,
    val subtitle: String,
    val shareId: String,
    val songs: List<Song>,
)

/** "3:45", or "1:02:05" for long ones. */
fun formatTime(milliseconds: Long): String {
    val seconds = (milliseconds.coerceAtLeast(0L) / 1000L).toInt()
    val hours = seconds / 3600
    val minutes = seconds % 3600 / 60
    val rest = seconds % 60
    return if (hours > 0) "%d:%02d:%02d".format(hours, minutes, rest) else "%d:%02d".format(minutes, rest)
}

/** A song's length, or a placeholder while it isn't known. */
fun formatDuration(seconds: Int): String = if (seconds <= 0) "–:––" else formatTime(seconds * 1000L)

fun formatBytes(bytes: Long): String = when {
    bytes >= 1L shl 30 -> "%.1f GB".format(bytes / (1L shl 30).toDouble())
    bytes >= 1L shl 20 -> "${bytes shr 20} MB"
    else -> "${bytes shr 10} KB"
}

fun formatTotal(seconds: Int): String {
    val minutes = (seconds + 30) / 60
    return if (minutes >= 60) "${minutes / 60} h ${minutes % 60} min" else "$minutes min"
}

fun songCount(count: Int): String = if (count == 1) "1 song" else "$count songs"

/** "12 songs, 47 min", saying "at least" while some lengths aren't known. */
fun summary(songs: List<Song>): String {
    val known = songs.filter { it.seconds > 0 }
    val total = known.sumOf { it.seconds }
    if (total < 60) return songCount(songs.size)
    val atLeast = if (known.size < songs.size) "at least " else ""
    return "${songCount(songs.size)}, $atLeast${formatTotal(total)}"
}

/** Codes from the computer app: a whole library, or one shared category. */
object Codes {
    private const val SHARE_PREFIX = "tapedeck:1:"
    private const val LIBRARY_PREFIX = "tapedeck:lib:1:"
    private const val MAX_BYTES = 4_000_000
    private val VIDEO_ID = Regex("[A-Za-z0-9_-]{11}")
    private val SHARE_ID = Regex("[0-9a-f]{12}")
    private val BODY = Regex("[A-Za-z0-9_-]+")
    val COLOR = Regex("#[0-9a-fA-F]{6}")

    class Parsed(val categories: List<Category>, val accent: String?)

    fun read(text: String): Parsed? {
        val compact = text.filterNot { it.isWhitespace() }  // chat apps sometimes wrap long codes
        unpack(compact, LIBRARY_PREFIX)?.let { json ->
            val list = json.optJSONArray("c") ?: return null
            val categories = (0 until minOf(list.length(), 500)).mapNotNull { index ->
                list.optJSONObject(index)?.let { category(it, full = true) }
            }
            return Parsed(categories, json.optString("a", "").takeIf { COLOR.matches(it) })
        }
        unpack(compact, SHARE_PREFIX)?.let { json ->
            return Parsed(listOf(category(json, full = false)), null)
        }
        return null
    }

    private fun unpack(compact: String, prefix: String): JSONObject? {
        if (!compact.startsWith(prefix, ignoreCase = true)) return null
        val body = compact.substring(prefix.length)
        if (!BODY.matches(body)) return null
        return try {
            val inflater = Inflater()
            inflater.setInput(Base64.getUrlDecoder().decode(body))
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(16_384)
            try {
                while (!inflater.finished()) {
                    val count = inflater.inflate(buffer)
                    if (count == 0 && (inflater.needsInput() || inflater.needsDictionary())) return null
                    out.write(buffer, 0, count)
                    if (out.size() > MAX_BYTES) return null
                }
            } finally {
                inflater.end()
            }
            JSONObject(String(out.toByteArray(), Charsets.UTF_8))
        } catch (e: Exception) {
            null
        }
    }

    // Share codes carry renamed titles only; library codes carry every title and channel.
    private fun category(json: JSONObject, full: Boolean): Category {
        val items = json.optJSONArray("s") ?: JSONArray()
        val seen = HashSet<String>()
        val songs = ArrayList<Song>()
        for (index in 0 until minOf(items.length(), 5000)) {
            val item = when (val raw = items.opt(index)) {
                is JSONArray -> raw
                is String -> JSONArray().put(raw)
                else -> continue
            }
            val videoId = item.optString(0, "")
            if (!VIDEO_ID.matches(videoId) || !seen.add(videoId)) continue
            val seconds = item.optInt(1, 0).takeIf { it in 1 until 360_000 } ?: 0
            val title = item.optString(2, "").trim().take(200)
            val channel = if (full) item.optString(3, "").trim().take(120) else ""
            songs.add(Song(videoId, title, channel, seconds))
        }
        val shareId = json.optString("i", "")
        return Category(
            id = UUID.randomUUID().toString().replace("-", "").take(12),
            title = json.optString("n", "").trim().take(80).ifEmpty { "Shared playlist" },
            subtitle = json.optString("d", "").trim().take(140),
            shareId = if (SHARE_ID.matches(shareId)) shareId else "",
            songs = songs,
        )
    }
}

/** The phone's library, kept in one JSON file. Changes publish through [categories]. */
class LibraryStore(context: Context) {
    private val file = File(context.filesDir, "library.json")
    private val _categories = MutableStateFlow<List<Category>>(emptyList())
    val categories: StateFlow<List<Category>> = _categories.asStateFlow()
    private val _accent = MutableStateFlow(DEFAULT_ACCENT)
    val accent: StateFlow<String> = _accent.asStateFlow()

    class ImportResult(val added: Int, val updated: Int)

    init {
        load()
    }

    /** Adds the code's categories; ones that came from the same computer category are updated to match. */
    @Synchronized
    fun importCode(text: String): ImportResult {
        val parsed = Codes.read(text)
            ?: throw IllegalArgumentException("That code is incomplete or damaged. Copy it again, all of it.")
        if (parsed.categories.all { it.songs.isEmpty() }) {
            throw IllegalArgumentException("That code has no songs in it.")
        }
        val list = _categories.value.toMutableList()
        var added = 0
        var updated = 0
        for (incoming in parsed.categories) {
            val index = if (incoming.shareId.isEmpty()) -1 else list.indexOfFirst { it.shareId == incoming.shareId }
            if (index < 0) {
                list.add(incoming)
                added += 1
                continue
            }
            val known = list[index].songs.associateBy { it.videoId }
            val songs = incoming.songs.map { song ->
                val old = known[song.videoId]
                if (old != null && song.title.isEmpty()) song.copy(title = old.title, channel = old.channel) else song
            }
            list[index] = list[index].copy(title = incoming.title, subtitle = incoming.subtitle, songs = songs)
            updated += 1
        }
        parsed.accent?.let { _accent.value = it }
        _categories.value = list
        save()
        return ImportResult(added, updated)
    }

    @Synchronized
    fun deleteCategory(id: String) {
        _categories.value = _categories.value.filterNot { it.id == id }
        save()
    }

    @Synchronized
    fun removeSong(categoryId: String, videoId: String) {
        _categories.value = _categories.value.map { category ->
            if (category.id == categoryId) category.copy(songs = category.songs.filterNot { it.videoId == videoId })
            else category
        }
        save()
    }

    /** Songs from share codes arrive without titles; these get looked up. */
    fun missingDetails(): List<String> =
        _categories.value.flatMap { category -> category.songs.filter { it.title.isEmpty() }.map { it.videoId } }.distinct()

    @Synchronized
    fun fillIn(found: Map<String, Pair<String, String>>) {
        if (found.isEmpty()) return
        _categories.value = _categories.value.map { category ->
            category.copy(songs = category.songs.map { song ->
                val details = found[song.videoId]
                if (details != null && song.title.isEmpty()) song.copy(title = details.first, channel = details.second)
                else song
            })
        }
        save()
    }

    private fun load() {
        if (!file.exists()) return
        try {
            val json = JSONObject(file.readText())
            json.optString("accent", "").takeIf { Codes.COLOR.matches(it) }?.let { _accent.value = it }
            val list = json.optJSONArray("categories") ?: return
            val categories = ArrayList<Category>()
            for (index in 0 until list.length()) {
                val item = list.optJSONObject(index) ?: continue
                val items = item.optJSONArray("songs") ?: JSONArray()
                val songs = ArrayList<Song>()
                for (position in 0 until items.length()) {
                    val song = items.optJSONArray(position) ?: continue
                    songs.add(Song(song.optString(0, ""), song.optString(2, ""), song.optString(3, ""), song.optInt(1, 0)))
                }
                categories.add(Category(item.optString("id", ""), item.optString("title", ""),
                    item.optString("subtitle", ""), item.optString("share_id", ""), songs))
            }
            _categories.value = categories
        } catch (e: Exception) {
            // Keep the unreadable file for a look later, and start fresh rather than crash.
            file.renameTo(File(file.parentFile, "library.broken.json"))
        }
    }

    private fun save() {
        val list = JSONArray()
        for (category in _categories.value) {
            val songs = JSONArray()
            for (song in category.songs) {
                songs.put(JSONArray().put(song.videoId).put(song.seconds).put(song.title).put(song.channel))
            }
            list.put(JSONObject().put("id", category.id).put("title", category.title)
                .put("subtitle", category.subtitle).put("share_id", category.shareId).put("songs", songs))
        }
        val text = JSONObject().put("version", 1).put("accent", _accent.value).put("categories", list).toString()
        val temp = File(file.parentFile, "library.json.tmp")
        temp.writeText(text)
        if (!temp.renameTo(file)) {  // write-then-rename, so a crash can't leave half a file
            file.delete()
            temp.renameTo(file)
        }
    }
}
