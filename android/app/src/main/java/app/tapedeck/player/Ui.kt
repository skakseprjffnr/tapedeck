package app.tapedeck.player

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings as AndroidSettings
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.DownloadDone
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.RepeatOne
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Shuffle
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import coil.compose.AsyncImage
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private val Background = Color(0xFF000000)
private val Panel = Color(0xFF0D0D0D)
private val Raised = Color(0xFF181818)
private val Line = Color(0xFF2A2A2A)
private val Smoke = Color(0xFF9A9A9A)
private val Danger = Color(0xFFE5534B)

private sealed interface Screen {
    data object Library : Screen
    data class Open(val categoryId: String) : Screen
    data object Import : Screen
    data object Settings : Screen
}

/** The computer's presets, in the same order. */
private val PRESETS = listOf(
    "#4592a2" to "Teal (default)", "#4f7cff" to "Blue", "#8b6cf6" to "Violet", "#e2609b" to "Pink",
    "#e5534b" to "Red", "#ea8a3a" to "Orange", "#d4b43a" to "Gold", "#4fb36a" to "Green",
)

/** Contrast against the black background (WCAG), as the computer app measures it. */
private fun contrastOnBlack(color: Color): Float = (color.luminance() + 0.05f) / 0.05f

/** Black or white text on the accent, whichever reads better, like the computer app. */
private fun onAccent(color: Color): Color =
    if (contrastOnBlack(color) >= 1.05f / (color.luminance() + 0.05f)) Color.Black else Color.White

private data class PlayerUi(
    val videoId: String? = null,
    val title: String = "",
    val channel: String = "",
    val wantsToPlay: Boolean = false,
    val position: Long = 0L,
    val duration: Long = 0L,
    val repeatOne: Boolean = false,
    val hasNext: Boolean = false,
    val error: String? = null,
)

private fun Player.snapshot(): PlayerUi {
    val item = currentMediaItem
    return PlayerUi(
        videoId = item?.mediaId,
        title = item?.mediaMetadata?.title?.toString().orEmpty(),
        channel = item?.mediaMetadata?.artist?.toString().orEmpty(),
        wantsToPlay = playWhenReady,
        position = currentPosition,
        duration = duration.coerceAtLeast(0L),
        repeatOne = repeatMode == Player.REPEAT_MODE_ONE,
        hasNext = hasNextMediaItem(),
        error = playerError?.let { it.cause?.message ?: it.message },
    )
}

/** Android's page for Tapedeck, which shows the storage its saved songs use. */
private fun openAppStorage(context: Context) {
    val intent = Intent(AndroidSettings.ACTION_APPLICATION_DETAILS_SETTINGS,
        android.net.Uri.parse("package:${context.packageName}"))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}

/** Tapedeck's page in the phone's notification settings. */
private fun openNotificationSettings(context: Context) {
    val intent = Intent(AndroidSettings.ACTION_APP_NOTIFICATION_SETTINGS)
        .putExtra(AndroidSettings.EXTRA_APP_PACKAGE, context.packageName)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}

private fun accentColor(hex: String): Color = try {
    Color(android.graphics.Color.parseColor(hex))
} catch (e: IllegalArgumentException) {
    Color(0xFF4592A2)
}

private fun Song.toMediaItem(): MediaItem = MediaItem.Builder()
    .setMediaId(videoId)
    .setUri(watchUrl)
    .setMediaMetadata(
        MediaMetadata.Builder()
            .setTitle(title.ifEmpty { "YouTube video" })
            .setArtist(channel)
            .setArtworkUri(android.net.Uri.parse(thumbnail))
            .build()
    )
    .build()

private fun playCategory(controller: MediaController?, category: Category, start: Int?, shuffle: Boolean) {
    if (controller == null || category.songs.isEmpty()) return
    val items = category.songs.map { it.toMediaItem() }
    controller.shuffleModeEnabled = shuffle
    val first = start ?: if (shuffle) items.indices.random() else 0
    controller.setMediaItems(items, first, 0L)
    controller.prepare()
    controller.play()
}

@Composable
fun TapedeckApp(store: LibraryStore, controller: MediaController?) {
    val categories by store.categories.collectAsState()
    val accentHex by store.accent.collectAsState()
    val accent = remember(accentHex) { accentColor(accentHex) }
    val scheme = remember(accent) {
        darkColorScheme(
            primary = accent,
            onPrimary = onAccent(accent),
            secondary = accent,
            background = Background,
            onBackground = Color.White,
            surface = Background,
            onSurface = Color.White,
            surfaceVariant = Raised,
            onSurfaceVariant = Smoke,
            outline = Line,
            error = Danger,
        )
    }
    var screen by remember { mutableStateOf<Screen>(Screen.Library) }
    val player = rememberPlayer(controller)
    val context = LocalContext.current
    val status by Offline.status.collectAsState()
    val saved by Offline.saved.collectAsState()
    val restricted by Offline.ageRestricted.collectAsState()
    var mobileData by remember { mutableStateOf(Offline.mobileDataAllowed(context)) }
    // Checked on every recomposition (each second while saving), so coming back from the
    // phone's settings shows the change.
    val notificationsOn = NotificationManagerCompat.from(context).areNotificationsEnabled()

    // Every song in the library gets saved; removed ones are forgotten. Titles filling in
    // don't change the set, so they don't trigger this.
    val songIds = remember(categories) {
        categories.flatMapTo(HashSet()) { category -> category.songs.map { it.videoId } }
    }
    LaunchedEffect(songIds) {
        delay(400)  // a burst of changes syncs once
        Offline.sync(context, store.categories.value)
    }
    LaunchedEffect(status.active) {  // live progress and speed while songs are saving
        while (status.active) {
            delay(1000)
            Offline.tick()
        }
    }
    // Android 13 and later ask before showing the saving notification; ask once.
    val askToNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    val stillSaving = status.enabled && status.saved + status.failed < status.total
    LaunchedEffect(stillSaving) {
        val preferences = Offline.preferences(context)
        if (stillSaving && Build.VERSION.SDK_INT >= 33 && !preferences.getBoolean("asked", false) &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            preferences.edit().putBoolean("asked", true).apply()
            askToNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    MaterialTheme(colorScheme = scheme) {
        Surface(color = Background, modifier = Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxSize().navigationBarsPadding()) {
                Box(Modifier.weight(1f)) {
                    when (val current = screen) {
                        Screen.Library -> LibraryScreen(
                            categories = categories,
                            status = status,
                            mobileData = mobileData,
                            onOpen = { screen = Screen.Open(it) },
                            onImport = { screen = Screen.Import },
                            onPlay = { category, shuffle -> playCategory(controller, category, null, shuffle) },
                            onSettings = { screen = Screen.Settings },
                            notificationsOn = notificationsOn,
                            onNotifications = { openNotificationSettings(context) },
                            onSaving = { on -> Offline.setSaving(context, on, store.categories.value) },
                            onMobileData = { allowed ->
                                mobileData = allowed
                                Offline.setMobileData(context, allowed)
                            },
                            onTryAgain = { Offline.sync(context, store.categories.value, retryFailed = true) },
                        )
                        is Screen.Open -> {
                            BackHandler { screen = Screen.Library }
                            val category = categories.firstOrNull { it.id == current.categoryId }
                            if (category == null) {
                                LaunchedEffect(current.categoryId) { screen = Screen.Library }
                            } else {
                                CategoryScreen(
                                    category = category,
                                    playingId = player.videoId,
                                    saved = saved,
                                    restricted = restricted,
                                    automatic = status.enabled,
                                    onSaveSongs = { songIds ->
                                        Offline.pin(context, songIds, store.categories.value)
                                        val what = if (songIds.size == 1) "the song" else songCount(songIds.size)
                                        Toast.makeText(context, "Saving $what to this phone", Toast.LENGTH_SHORT).show()
                                    },
                                    onUnsaveSongs = { songIds ->
                                        Offline.unpin(context, songIds, store.categories.value)
                                    },
                                    store = store,
                                    onBack = { screen = Screen.Library },
                                    onPlay = { index, shuffle -> playCategory(controller, category, index, shuffle) },
                                )
                            }
                        }
                        Screen.Import -> {
                            BackHandler { screen = Screen.Library }
                            ImportScreen(store = store, onDone = { screen = Screen.Library })
                        }
                        Screen.Settings -> {
                            BackHandler { screen = Screen.Library }
                            SettingsScreen(
                                store = store,
                                status = status,
                                mobileData = mobileData,
                                onBack = { screen = Screen.Library },
                                onSaving = { on -> Offline.setSaving(context, on, store.categories.value) },
                                onMobileData = { allowed ->
                                    mobileData = allowed
                                    Offline.setMobileData(context, allowed)
                                },
                                onClearCache = { resave -> Offline.clearCache(context, store.categories.value, resave) },
                                notificationsOn = notificationsOn,
                                onNotifications = { openNotificationSettings(context) },
                            )
                        }
                    }
                }
                if (player.videoId != null) MiniPlayer(player, controller)
            }
        }
    }
}

@Composable
private fun rememberPlayer(controller: MediaController?): PlayerUi {
    var state by remember { mutableStateOf(PlayerUi()) }
    DisposableEffect(controller) {
        val listener = object : Player.Listener {
            override fun onEvents(player: Player, events: Player.Events) {
                state = player.snapshot()
            }
        }
        controller?.addListener(listener)
        state = controller?.snapshot() ?: PlayerUi()
        onDispose { controller?.removeListener(listener) }
    }
    LaunchedEffect(controller, state.wantsToPlay) {  // the progress bar, twice a second while playing
        while (controller != null && state.wantsToPlay) {
            delay(500)
            state = state.copy(position = controller.currentPosition, duration = controller.duration.coerceAtLeast(0L))
        }
    }
    return state
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LibraryScreen(
    categories: List<Category>,
    status: SaveStatus,
    mobileData: Boolean,
    onOpen: (String) -> Unit,
    onImport: () -> Unit,
    onPlay: (Category, Boolean) -> Unit,
    onSettings: () -> Unit,
    notificationsOn: Boolean,
    onNotifications: () -> Unit,
    onSaving: (Boolean) -> Unit,
    onMobileData: (Boolean) -> Unit,
    onTryAgain: () -> Unit,
) {
    Scaffold(
        containerColor = Background,
        topBar = {
            TopAppBar(
                title = { Text("tapedeck", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = onImport) { Icon(Icons.Filled.Add, contentDescription = "Import") }
                    IconButton(onClick = onSettings) { Icon(Icons.Filled.Settings, contentDescription = "Settings") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Background),
            )
        },
    ) { padding ->
        if (categories.isEmpty()) {
            Column(
                modifier = Modifier.fillMaxSize().padding(padding).padding(28.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text("Nothing here yet", style = MaterialTheme.typography.titleLarge)
                Spacer(Modifier.height(8.dp))
                Text(
                    "Copy your playlists from Tapedeck on your computer: click the phone button at " +
                        "the top of its sidebar, then scan the QR code it shows.",
                    color = Smoke,
                    textAlign = TextAlign.Center,
                )
                Spacer(Modifier.height(20.dp))
                Button(onClick = onImport) { Text("Import") }
            }
        } else {
            val total = remember(categories) { categories.flatMap { it.songs } }
            LazyColumn(contentPadding = padding, modifier = Modifier.fillMaxSize()) {
                item {
                    val count = categories.size
                    Text(
                        "$count ${if (count == 1) "category" else "categories"}, ${summary(total)}",
                        color = Smoke,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp),
                    )
                }
                if (status.total > 0) {
                    item {
                        SavingPanel(status, onScan = onImport, notificationsOn = notificationsOn,
                            onNotifications = onNotifications,
                            onAllowMobileData = { onMobileData(true) }, onTurnOn = { onSaving(true) },
                            onTryAgain = onTryAgain)
                    }
                }
                items(categories, key = { it.id }) { category ->
                    CategoryCard(category, onOpen = { onOpen(category.id) }, onPlay = { onPlay(category, it) })
                }
                item { Spacer(Modifier.height(12.dp)) }
            }
        }
    }
}

/** How far along saving is: a progress bar until every song is on the phone. */
@Composable
private fun SavingPanel(
    status: SaveStatus,
    onScan: () -> Unit,
    notificationsOn: Boolean,
    onNotifications: () -> Unit,
    onAllowMobileData: () -> Unit,
    onTurnOn: () -> Unit,
    onTryAgain: () -> Unit,
) {
    val accent = MaterialTheme.colorScheme.primary
    Column(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 6.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(Panel)
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        if (status.saved >= status.total) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.DownloadDone, contentDescription = null, tint = accent, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text("All ${songCount(status.total)} are on this phone (${formatBytes(status.bytes)}) and play offline.",
                    color = Smoke, style = MaterialTheme.typography.bodySmall)
            }
        } else {
            Text(
                if (status.enabled) "Saving songs for offline: ${status.saved} of ${status.total}"
                else "${status.saved} of ${status.total} songs saved for offline",
                style = MaterialTheme.typography.bodyMedium,
            )
            LinearProgressIndicator(
                progress = { status.progress },
                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp).height(4.dp),
                color = accent,
                trackColor = Line,
            )
            val sources = buildList {
                if (status.computerSpeed > 0) add("from your computer at ${formatSpeed(status.computerSpeed)}")
                if (status.youtubeSpeed > 0) add("from YouTube at ${formatSpeed(status.youtubeSpeed)}")
            }
            val detail = when {
                !status.enabled -> "Saving the songs you chose."
                status.waitingForNetwork -> "Waiting for an internet connection."
                status.waitingForWifi -> "Waiting for Wi-Fi."
                sources.isNotEmpty() -> "Copying ${sources.joinToString(" and ")}."
                else -> "Saved songs play offline; the rest stream meanwhile."
            }
            Text("$detail ${formatBytes(status.bytes)} used.", color = Smoke, style = MaterialTheme.typography.bodySmall)
            if (!status.enabled) {
                TextButton(onClick = onTurnOn) { Text("Save every song") }
            } else if (status.waitingForWifi && !status.waitingForNetwork) {
                TextButton(onClick = onAllowMobileData) { Text("Use mobile data too") }
            } else if (status.youtubeSpeed > 0 && !status.computerConnected) {
                Text("Songs saved on your computer copy much faster from there: open Copy to your " +
                    "phone on it, then scan its code.", color = Smoke,
                    style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
                TextButton(onClick = onScan) { Text("Scan the computer's code") }
            } else if (!notificationsOn) {
                Text("Notifications are off, so progress only shows here.", color = Smoke,
                    style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
                TextButton(onClick = onNotifications) { Text("Show progress in notifications") }
            }
            if (status.ageRestricted > 0) {
                Text("${songCount(status.ageRestricted)} ${if (status.ageRestricted == 1) "is" else "are"} " +
                    "age-restricted, so only your computer can send them: sign in there (Settings, " +
                    "YouTube account), let it save them, then scan its code.",
                    color = Smoke, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 6.dp))
                TextButton(onClick = onScan) { Text("Scan the computer's code") }
            }
            if (status.failed > 0) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("${status.failed} couldn't be saved.", color = Danger, style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.weight(1f))
                    TextButton(onClick = onTryAgain) { Text("Try again") }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CategoryCard(category: Category, onOpen: () -> Unit, onPlay: (Boolean) -> Unit) {
    Card(
        onClick = onOpen,
        colors = CardDefaults.cardColors(containerColor = Panel),
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp),
    ) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(category.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (category.subtitle.isNotEmpty()) {
                    Text(category.subtitle, color = Smoke, style = MaterialTheme.typography.bodyMedium,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                Text(summary(category.songs), color = Smoke, style = MaterialTheme.typography.bodySmall)
            }
            IconButton(onClick = { onPlay(true) }, enabled = category.songs.isNotEmpty()) {
                Icon(Icons.Filled.Shuffle, contentDescription = "Shuffle")
            }
            FilledIconButton(onClick = { onPlay(false) }, enabled = category.songs.isNotEmpty()) {
                Icon(Icons.Filled.PlayArrow, contentDescription = "Play")
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CategoryScreen(
    category: Category,
    playingId: String?,
    saved: Set<String>,
    restricted: Set<String>,
    automatic: Boolean,
    onSaveSongs: (List<String>) -> Unit,
    onUnsaveSongs: (List<String>) -> Unit,
    store: LibraryStore,
    onBack: () -> Unit,
    onPlay: (Int?, Boolean) -> Unit,
) {
    var menu by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }
    Scaffold(
        containerColor = Background,
        topBar = {
            TopAppBar(
                title = {},
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = {
                    Box {
                        IconButton(onClick = { menu = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "More")
                        }
                        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                            val songIds = category.songs.map { it.videoId }
                            DropdownMenuItem(
                                text = { Text("Save all to this phone") },
                                enabled = songIds.any { it !in saved },
                                onClick = {
                                    menu = false
                                    onSaveSongs(songIds)
                                },
                            )
                            if (!automatic && songIds.any { it in saved }) {
                                DropdownMenuItem(
                                    text = { Text("Remove all from this phone") },
                                    onClick = {
                                        menu = false
                                        onUnsaveSongs(songIds)
                                    },
                                )
                            }
                            DropdownMenuItem(
                                text = { Text("Delete this category") },
                                onClick = {
                                    menu = false
                                    confirmDelete = true
                                },
                            )
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Background),
            )
        },
    ) { padding ->
        LazyColumn(contentPadding = padding, modifier = Modifier.fillMaxSize()) {
            item {
                Column(Modifier.padding(horizontal = 20.dp, vertical = 4.dp)) {
                    Text(category.title, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                    if (category.subtitle.isNotEmpty()) Text(category.subtitle, color = Smoke)
                    Text(summary(category.songs), color = Smoke, style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(14.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Button(onClick = { onPlay(0, false) }, enabled = category.songs.isNotEmpty()) {
                            Icon(Icons.Filled.PlayArrow, contentDescription = null)
                            Spacer(Modifier.width(6.dp))
                            Text("Play")
                        }
                        OutlinedButton(onClick = { onPlay(null, true) }, enabled = category.songs.isNotEmpty()) {
                            Icon(Icons.Filled.Shuffle, contentDescription = null)
                            Spacer(Modifier.width(6.dp))
                            Text("Shuffle")
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                }
            }
            itemsIndexed(category.songs, key = { _, song -> song.videoId }) { index, song ->
                SongRow(
                    position = index + 1,
                    song = song,
                    playing = song.videoId == playingId,
                    saved = song.videoId in saved,
                    restricted = song.videoId in restricted && song.videoId !in saved,
                    onSave = { onSaveSongs(listOf(song.videoId)) },
                    onUnsave = if (!automatic) ({ onUnsaveSongs(listOf(song.videoId)) }) else null,
                    onClick = { onPlay(index, false) },
                    onRemove = { store.removeSong(category.id, song.videoId) },
                )
            }
            item { Spacer(Modifier.height(12.dp)) }
        }
    }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete ${category.title}?") },
            text = { Text("Its songs are removed from this phone. Your computer keeps its own copy.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    store.deleteCategory(category.id)
                    onBack()
                }) { Text("Delete", color = Danger) }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = false }) { Text("Cancel") }
            },
            containerColor = Raised,
        )
    }
}

@Composable
private fun SongRow(
    position: Int,
    song: Song,
    playing: Boolean,
    saved: Boolean,
    restricted: Boolean,
    onSave: () -> Unit,
    onUnsave: (() -> Unit)?,
    onClick: () -> Unit,
    onRemove: () -> Unit,
) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    var menu by remember { mutableStateOf(false) }
    val accent = MaterialTheme.colorScheme.primary
    fun copy(link: String, name: String) {
        clipboard.setText(AnnotatedString(link))
        Toast.makeText(context, "$name link copied", Toast.LENGTH_SHORT).show()
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .background(if (playing) Raised else Color.Transparent)
            .clickable(onClick = onClick)
            .padding(start = 16.dp, top = 6.dp, bottom = 6.dp),
    ) {
        Text(position.toString(), color = if (playing) accent else Smoke,
            style = MaterialTheme.typography.bodySmall, modifier = Modifier.width(30.dp))
        AsyncImage(
            model = song.thumbnail,
            contentDescription = null,
            contentScale = ContentScale.Crop,
            modifier = Modifier.size(width = 64.dp, height = 36.dp).clip(RoundedCornerShape(6.dp)).background(Raised),
        )
        Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
            Text(song.title.ifEmpty { "Looking up the title…" }, color = if (playing) accent else Color.White,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (restricted) {
                Text("Age-restricted: it can come from your computer", color = Smoke,
                    style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            } else if (song.channel.isNotEmpty()) {
                Text(song.channel, color = Smoke, style = MaterialTheme.typography.bodySmall,
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        if (saved) {
            Icon(Icons.Filled.DownloadDone, contentDescription = "Saved on this phone", tint = Smoke,
                modifier = Modifier.size(15.dp))
            Spacer(Modifier.width(6.dp))
        }
        Text(formatDuration(song.seconds), color = Smoke, style = MaterialTheme.typography.bodySmall)
        Box {
            IconButton(onClick = { menu = true }) {
                Icon(Icons.Filled.MoreVert, contentDescription = "More", tint = Smoke)
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                DropdownMenuItem(text = { Text("Copy YouTube link") }, onClick = {
                    menu = false
                    copy(song.watchUrl, "YouTube")
                })
                DropdownMenuItem(text = { Text("Copy YouTube Music link") }, onClick = {
                    menu = false
                    copy(song.musicUrl, "YouTube Music")
                })
                if (!saved) {
                    DropdownMenuItem(text = { Text("Save to this phone") }, onClick = {
                        menu = false
                        onSave()
                    })
                } else if (onUnsave != null) {
                    DropdownMenuItem(text = { Text("Remove from this phone") }, onClick = {
                        menu = false
                        onUnsave()
                    })
                }
                DropdownMenuItem(text = { Text("Remove from this category") }, onClick = {
                    menu = false
                    onRemove()
                })
            }
        }
    }
}

@Composable
private fun MiniPlayer(state: PlayerUi, controller: MediaController?) {
    val accent = MaterialTheme.colorScheme.primary
    // While dragging, the bar and the time follow the finger; the song jumps there on release.
    var dragging by remember { mutableStateOf<Float?>(null) }
    val length = state.duration
    val played = if (length > 0) (state.position.toFloat() / length).coerceIn(0f, 1f) else 0f
    val shown = dragging ?: played
    Column(Modifier.fillMaxWidth().background(Panel)) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(start = 12.dp, end = 4.dp, top = 8.dp, bottom = 8.dp),
        ) {
            AsyncImage(
                model = "https://i.ytimg.com/vi/${state.videoId}/mqdefault.jpg",
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.size(width = 56.dp, height = 32.dp).clip(RoundedCornerShape(5.dp)).background(Raised),
            )
            Column(Modifier.weight(1f).padding(horizontal = 10.dp)) {
                Text(state.title, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(state.error ?: state.channel, color = if (state.error != null) Danger else Smoke,
                    style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            IconButton(onClick = { controller?.seekToPrevious() }) {
                Icon(Icons.Filled.SkipPrevious, contentDescription = "Previous")
            }
            FilledIconButton(onClick = {
                controller?.let {
                    if (it.playWhenReady) {
                        it.pause()
                    } else {
                        if (it.playbackState == Player.STATE_IDLE) it.prepare()  // retry after an error
                        it.play()
                    }
                }
            }) {
                Icon(if (state.wantsToPlay) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                    contentDescription = if (state.wantsToPlay) "Pause" else "Play")
            }
            IconButton(onClick = { controller?.seekToNextMediaItem() }, enabled = state.hasNext) {
                Icon(Icons.Filled.SkipNext, contentDescription = "Next")
            }
            IconButton(onClick = {
                controller?.let {
                    it.repeatMode = if (it.repeatMode == Player.REPEAT_MODE_ONE) Player.REPEAT_MODE_OFF
                    else Player.REPEAT_MODE_ONE
                }
            }) {
                Icon(Icons.Filled.RepeatOne, contentDescription = "Repeat this song", tint = if (state.repeatOne) accent else Smoke)
            }
        }
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(start = 14.dp, end = 14.dp, bottom = 4.dp),
        ) {
            Text(formatTime(if (dragging != null) (shown * length).toLong() else state.position),
                color = Smoke, style = MaterialTheme.typography.labelSmall, modifier = Modifier.width(44.dp))
            Slider(
                value = shown,
                onValueChange = { dragging = it },
                onValueChangeFinished = {
                    val target = dragging
                    if (target != null && length > 0) controller?.seekTo((target * length).toLong())
                    dragging = null
                },
                enabled = controller != null && length > 0,
                colors = SliderDefaults.colors(
                    thumbColor = accent,
                    activeTrackColor = accent,
                    inactiveTrackColor = Line,
                ),
                modifier = Modifier.weight(1f).height(30.dp),
            )
            Text(formatTime(length), color = Smoke, style = MaterialTheme.typography.labelSmall,
                textAlign = TextAlign.End, modifier = Modifier.width(44.dp))
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ImportScreen(store: LibraryStore, onDone: () -> Unit) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    var text by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var problem by remember { mutableStateOf<String?>(null) }

    fun submit(value: String) {
        if (value.isBlank() || busy) return
        busy = true
        problem = null
        scope.launch {
            val outcome = withContext(Dispatchers.IO) { runCatching { Importer.run(store, value) } }
            busy = false
            outcome
                .onSuccess { message ->
                    Toast.makeText(context, message, Toast.LENGTH_LONG).show()
                    onDone()
                }
                .onFailure { problem = it.message ?: "That didn't work. Try again." }
        }
    }

    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        val contents = result.contents
        if (contents != null) {
            text = contents
            submit(contents)
        }
    }

    Scaffold(
        containerColor = Background,
        topBar = {
            TopAppBar(
                title = { Text("Import") },
                navigationIcon = {
                    IconButton(onClick = onDone) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Background),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text(
                "On your computer, click the phone button at the top of Tapedeck's sidebar. Then scan " +
                    "the QR code it shows, or paste the code it copies.",
                color = Smoke,
            )
            Button(
                onClick = {
                    scanner.launch(
                        ScanOptions()
                            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                            .setPrompt("Point your camera at the QR code on your computer")
                            .setBeepEnabled(false)
                            .setOrientationLocked(false)
                    )
                },
                enabled = !busy,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Icon(Icons.Filled.QrCodeScanner, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Scan QR code")
            }
            OutlinedTextField(
                value = text,
                onValueChange = {
                    text = it
                    problem = null
                },
                label = { Text("Or paste a library or share code") },
                enabled = !busy,
                maxLines = 6,
                modifier = Modifier.fillMaxWidth().heightIn(min = 120.dp),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(
                    onClick = {
                        text = clipboard.getText()?.text.orEmpty()
                        problem = null
                    },
                    enabled = !busy,
                ) {
                    Icon(Icons.Filled.ContentPaste, contentDescription = null)
                    Spacer(Modifier.width(6.dp))
                    Text("Paste")
                }
                Button(onClick = { submit(text) }, enabled = text.isNotBlank() && !busy) { Text("Import") }
            }
            if (busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            problem?.let { Text(it, color = Danger) }
        }
    }
}


@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SettingsScreen(
    store: LibraryStore,
    status: SaveStatus,
    mobileData: Boolean,
    onBack: () -> Unit,
    onSaving: (Boolean) -> Unit,
    onMobileData: (Boolean) -> Unit,
    onClearCache: (Boolean) -> Unit,
    notificationsOn: Boolean,
    onNotifications: () -> Unit,
) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val accentHex by store.accent.collectAsState()
    val matchComputer by store.matchComputer.collectAsState()
    var typed by remember { mutableStateOf(accentHex) }
    LaunchedEffect(accentHex) {  // a preset or a reset updates the field
        if (normalizeHex(typed) != accentHex) typed = accentHex
    }
    var confirmClear by remember { mutableStateOf<String?>(null) }  // "clear" or "resave"
    var audioFormat by remember { mutableStateOf(Offline.audioFormat(context)) }
    var audioQuality by remember { mutableStateOf(Offline.audioQuality(context)) }
    val accent = MaterialTheme.colorScheme.primary
    val typedColor = normalizeHex(typed)
    val (hint, warn) = when {
        typedColor == null -> "That isn't a hex color yet: six digits, like #4592a2." to true
        contrastOnBlack(accentColor(typedColor)) < 3f ->
            "That's hard to see on Tapedeck's black background. A lighter color works better." to true
        else -> "Type a hex code like #4592a2, or pick a color." to false
    }
    Scaffold(
        containerColor = Background,
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Background),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Accent color", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            // A preview of what the color touches: buttons, progress, checks.
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(14.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(14.dp))
                    .background(Panel)
                    .padding(14.dp),
            ) {
                FilledIconButton(onClick = {}) { Icon(Icons.Filled.PlayArrow, contentDescription = null) }
                Column(Modifier.weight(1f)) {
                    Text("Now playing", color = accent, fontWeight = FontWeight.SemiBold)
                    LinearProgressIndicator(
                        progress = { 0.62f },
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp).height(4.dp),
                        color = accent,
                        trackColor = Line,
                    )
                }
                Icon(Icons.Filled.DownloadDone, contentDescription = null, tint = accent)
            }
            for (row in PRESETS.chunked(4)) {  // two rows of four fit every phone
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    for ((hex, name) in row) {
                        val selected = hex == accentHex
                        Box(
                            Modifier
                                .size(40.dp)
                                .clip(CircleShape)
                                .background(accentColor(hex))
                                .then(if (selected) Modifier.border(3.dp, Color.White, CircleShape) else Modifier)
                                .clickable { store.setAccent(hex) }
                                .semantics { contentDescription = name },
                        )
                    }
                }
            }
            OutlinedTextField(
                value = typed,
                onValueChange = { value ->
                    typed = value
                    normalizeHex(value)?.let { store.setAccent(it) }
                },
                label = { Text("Hex color") },
                placeholder = { Text(DEFAULT_ACCENT) },
                singleLine = true,
                isError = warn,
                modifier = Modifier.fillMaxWidth(),
            )
            Text(hint, color = if (warn) Danger else Smoke, style = MaterialTheme.typography.bodySmall)
            SettingSwitch(
                title = "Use my computer's color",
                detail = "Copying your library brings its accent color along.",
                checked = matchComputer,
                onChange = { store.setMatchComputer(it) },
            )
            TextButton(onClick = { store.resetAccent() }) { Text("Back to the default teal") }

            HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = 4.dp))
            Text("Audio quality", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text("Format", style = MaterialTheme.typography.bodyMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for ((key, label) in listOf("auto" to "Automatic", "opus" to "Opus", "aac" to "AAC")) {
                    FilterChip(
                        selected = audioFormat == key,
                        onClick = {
                            audioFormat = key
                            Offline.setAudio(context, audioFormat, audioQuality)
                        },
                        label = { Text(label) },
                    )
                }
            }
            Text("Quality", style = MaterialTheme.typography.bodyMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for ((key, label) in listOf("best" to "Best", "medium" to "128 kbps", "small" to "64 kbps")) {
                    FilterChip(
                        selected = audioQuality == key,
                        onClick = {
                            audioQuality = key
                            Offline.setAudio(context, audioFormat, audioQuality)
                        },
                        label = { Text(label) },
                    )
                }
            }
            Text("Songs saved from YouTube from now on use this; songs already saved keep theirs. " +
                "Songs copied from your computer come in its format (set it there, where MP3 is " +
                "also possible).", color = Smoke, style = MaterialTheme.typography.bodySmall)

            HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = 4.dp))
            Text("Saving songs", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            SettingSwitch(
                title = "Save every song automatically",
                detail = "Otherwise only songs you save by hand (from a song's or a category's menu) are kept.",
                checked = status.enabled,
                onChange = onSaving,
            )
            SettingSwitch(
                title = "Use mobile data for saving",
                detail = "Otherwise it waits for Wi-Fi. Each song is about 3 to 5 MB.",
                checked = mobileData,
                onChange = onMobileData,
            )
            SettingSwitch(
                title = "Progress notification",
                detail = if (notificationsOn) "Shows how saving is going while the app is closed."
                else "Off in your phone's settings. Tap to turn it on there.",
                checked = notificationsOn,
                onChange = { onNotifications() },
            )
            Text("${formatBytes(status.bytes)} used by saved songs. ${status.saved} of ${status.total} " +
                "saved, ${status.downloading} saving now, ${status.queued} waiting, ${status.failed} failed.",
                color = Smoke, style = MaterialTheme.typography.bodySmall)
            OutlinedButton(onClick = {
                clipboard.setText(AnnotatedString(Offline.report(notificationsOn)))
                Toast.makeText(context, "Saving details copied", Toast.LENGTH_SHORT).show()
            }) { Text("Copy saving details") }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = { confirmClear = "clear" }) { Text("Clear cache", color = Danger) }
                OutlinedButton(onClick = { confirmClear = "resave" }) { Text("Clear and re-save all") }
            }

            HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = 4.dp))
            Text("Saved songs folder", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(Offline.folderPath(context), style = MaterialTheme.typography.bodySmall)
            Text("${songCount(status.saved)}, ${formatBytes(status.bytes)}. They're in Tapedeck's own " +
                "storage, in the player's own format, so only Tapedeck can play them and file " +
                "managers can't open the folder. Android's page for Tapedeck shows the space they use.",
                color = Smoke, style = MaterialTheme.typography.bodySmall)
            OutlinedButton(onClick = { openAppStorage(context) }) { Text("Show in Android settings") }
            Spacer(Modifier.height(12.dp))
        }
    }
    confirmClear?.let { action ->
        val resave = action == "resave"
        val formatName = mapOf("auto" to "the best available format", "opus" to "Opus", "aac" to "AAC")[audioFormat]
            ?: "the best available format"
        val qualityName = mapOf("best" to "best quality", "medium" to "about 128 kbps", "small" to "about 64 kbps")[audioQuality]
            ?: "best quality"
        AlertDialog(
            onDismissRequest = { confirmClear = null },
            title = { Text(if (resave) "Re-save every song?" else "Clear the cache?") },
            text = {
                Text(
                    if (resave) {
                        "Every stored song is deleted and saved again in $formatName, $qualityName: " +
                            "${songCount(status.total.coerceAtLeast(status.saved))} to download. Songs " +
                            "your computer sends (scan its code while this runs) come in its format. " +
                            "Your library stays as it is."
                    } else {
                        "Saved and partly played songs, song images and remembered stream addresses " +
                            "are deleted from this phone. Saving every song automatically turns off, so " +
                            "they aren't saved again straight away. Your library stays as it is."
                    },
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = null
                    onClearCache(resave)
                    Toast.makeText(context, if (resave) "Re-saving every song" else "Cache cleared",
                        Toast.LENGTH_SHORT).show()
                }) { Text(if (resave) "Clear and re-save" else "Clear", color = Danger) }
            },
            dismissButton = {
                TextButton(onClick = { confirmClear = null }) { Text("Cancel") }
            },
            containerColor = Raised,
        )
    }
}

@Composable
private fun SettingSwitch(title: String, detail: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(vertical = 4.dp),
    ) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title)
            Text(detail, color = Smoke, style = MaterialTheme.typography.bodySmall)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
