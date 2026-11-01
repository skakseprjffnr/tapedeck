package app.tapedeck.player

import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.TransferListener
import java.io.IOException

private const val UNSET = -1L
private const val PIECE = 8L shl 20  // YouTube sends pieces up to about 12 MB at full speed

/**
 * Reads YouTube's audio in bounded pieces.
 *
 * YouTube's servers send a bounded piece at full speed but throttle one open-ended request
 * to about 32 KB/s; YouTube's own player and yt-dlp fetch in pieces too. Most songs fit in
 * one piece. Anything that isn't YouTube (the computer's transfer) passes straight through.
 */
@OptIn(UnstableApi::class)
class ChunkedDataSource(private val upstream: DataSource) : DataSource {
    private var spec: DataSpec? = null
    private var chunking = false
    private var opened = false
    private var next = 0L               // where the next byte comes from
    private var stop = UNSET            // where to stop (exclusive), once known
    private var pieceLeft = 0L          // bytes still to read from the open piece
    private var lastPieceShort = false  // the server sent less than asked: that was the end

    override fun addTransferListener(transferListener: TransferListener) {
        upstream.addTransferListener(transferListener)
    }

    override fun open(dataSpec: DataSpec): Long {
        chunking = dataSpec.uri.host?.endsWith(".googlevideo.com") == true
        opened = true
        if (!chunking) return upstream.open(dataSpec)
        spec = dataSpec
        next = dataSpec.position
        stop = if (dataSpec.length != C.LENGTH_UNSET.toLong()) dataSpec.position + dataSpec.length else UNSET
        val size = dataSpec.uri.getQueryParameter("clen")?.toLongOrNull()  // YouTube puts the size in the address
        if (size != null && size > 0 && (stop == UNSET || stop > size)) stop = size
        if (stop != UNSET && next >= stop) {
            pieceLeft = 0L
            lastPieceShort = true
            return 0L
        }
        openPiece()
        return if (stop != UNSET) stop - dataSpec.position else C.LENGTH_UNSET.toLong()
    }

    private fun openPiece() {
        val base = spec ?: throw IOException("Not open")
        val wanted = if (stop != UNSET) minOf(PIECE, stop - next) else PIECE
        val length = try {
            upstream.open(base.subrange(next - base.position, wanted))
        } catch (e: HttpDataSource.InvalidResponseCodeException) {
            // With no size known, a song that's an exact number of pieces long ends with a
            // request for one more: "nothing there" (416) then just means it's finished.
            if (e.responseCode != 416 || stop != UNSET || next == base.position) throw e
            pieceLeft = 0L
            lastPieceShort = true
            stop = next
            return
        }
        pieceLeft = if (length != C.LENGTH_UNSET.toLong()) length else wanted
        lastPieceShort = pieceLeft < wanted
        if (stop == UNSET) {  // the full size, from "Content-Range: bytes 0-8388607/12345678"
            totalSize(upstream.responseHeaders)?.let { stop = it }
        }
    }

    private fun totalSize(headers: Map<String, List<String>>): Long? {
        for ((name, values) in headers) {
            @Suppress("SENSELESS_COMPARISON")  // the map comes from Java and can hold a null name
            if (name != null && name.equals("Content-Range", ignoreCase = true)) {
                return values.firstOrNull()?.substringAfterLast('/')?.trim()?.toLongOrNull()
            }
        }
        return null
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (!chunking) return upstream.read(buffer, offset, length)
        if (length == 0) return 0
        while (true) {
            if (stop != UNSET && next >= stop) return C.RESULT_END_OF_INPUT
            if (pieceLeft == 0L) {
                if (stop == UNSET && lastPieceShort) return C.RESULT_END_OF_INPUT
                upstream.close()
                openPiece()
                if (pieceLeft == 0L) return C.RESULT_END_OF_INPUT  // nothing more came
                continue
            }
            val count = upstream.read(buffer, offset, minOf(length.toLong(), pieceLeft).toInt())
            if (count == C.RESULT_END_OF_INPUT) {
                if (stop == UNSET) return C.RESULT_END_OF_INPUT  // without a known size, this is the end
                throw IOException("YouTube ended the song early")
            }
            next += count
            pieceLeft -= count
            return count
        }
    }

    override fun getUri(): Uri? = upstream.uri

    override fun getResponseHeaders(): Map<String, List<String>> = upstream.responseHeaders

    override fun close() {
        if (opened) {
            opened = false
            spec = null
            upstream.close()
        }
    }
}
