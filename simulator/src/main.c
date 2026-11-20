#ifdef __APPLE__
#define _DARWIN_C_SOURCE
#else
#define _POSIX_C_SOURCE 200809L
#endif

/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <SDL.h>

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lvgl.h"
#include "src/drivers/sdl/lv_sdl_window.h"

#include "muse_state.h"
#include "muse_ui.h"
#include "sim_board.h"
#include "sim_platform.h"
#include "esp_timer.h"
#include "sim_services.h"
#include "sim_damage.h"

#define FRAME_STEP_MS 5

static bool apply_setting(const char *, const char *, bool);
static bool read_settings(const char *, bool, bool);
static volatile sig_atomic_t s_quit;
static int64_t s_last_poll;
static bool s_live_state;
static bool s_talk_active;
static time_t s_state_mtime;
static long s_state_nsec;

static const char *runtime_dir(void)
{
    const char *r = getenv("MUSE_UI_RUNTIME");
    return r ? r : "/run/muse-ui";
}

static void send_action(const char *action)
{
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0) return;
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s/input.sock", runtime_dir());
    char data[64];
    snprintf(data, sizeof(data), "{\"action\":\"%s\"}", action);
    (void)sendto(fd, data, strlen(data), MSG_DONTWAIT, (struct sockaddr *)&addr, sizeof(addr));
    close(fd);
}

static void send_pet_command(const char *command)
{
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0) return;
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s/input.sock", runtime_dir());
    char data[160];
    snprintf(data, sizeof(data), "{\"action\":\"pet_command\",\"command\":\"%s\"}", command);
    (void)sendto(fd, data, strlen(data), MSG_DONTWAIT, (struct sockaddr *)&addr, sizeof(addr));
    close(fd);
}


void muse_backend_desktop(void)
{
    send_action("hangup");
    if (s_talk_active) {
        s_talk_active = false;
        send_action("stop");
    }
    s_quit = 1;
}

void muse_backend_mute(void)
{
    send_action("mute");
}

void muse_backend_talk(bool active)
{
    if (active == s_talk_active) return;
    s_talk_active = active;
    send_action(active ? "start" : "stop");
}

void muse_backend_pet_action(const char *action)
{
    if (action && *action) {
        send_pet_command(action);
    }
}


static void poll_live_state(void)
{
    int64_t now = esp_timer_get_time();
    if (now - s_last_poll < 100000) return;
    s_last_poll = now;
    char path[256];
    snprintf(path, sizeof(path), "%s/state", runtime_dir());
    struct stat st;
    if (stat(path, &st) != 0 || time(NULL) - st.st_mtime > 20) {
        muse_state_set_mode(MUSE_MODE_ERROR);
        muse_state_set_caption("Muse service unavailable");
        sim_services_set_link_state(MUSE_LINK_OFFLINE);
        return;
    }
#ifdef __APPLE__
    long nsec = st.st_mtimespec.tv_nsec;
#else
    long nsec = st.st_mtim.tv_nsec;
#endif
    if (st.st_mtime == s_state_mtime && nsec == s_state_nsec) return;
    s_state_mtime = st.st_mtime;
    s_state_nsec = nsec;
    (void)read_settings(path, false, false);
}

static bool s_ready;
static float s_level;
static float s_progress;
static muse_power_t s_power = {
    .battery_pct = -1,
    .battery_mv = 0,
    .charging = false,
    .usb = true,
};
static muse_ble_state_t s_ble_state = MUSE_BLE_CONNECTED;
static uint32_t s_passkey;
static char s_ble_name[32] = "RDK X5";

static void usage(FILE *out, const char *argv0)
{
    fprintf(out,
            "Usage: %s [--headless] [--scenario FILE] [--run-ms N] "
            "[--screenshot FILE.ppm]\n"
            "\n"
            "Scenario lines are key=value. Supported keys:\n"
            "  face=boot|idle|listening|thinking|speaking|error|off|happy\n"
            "  caption=TEXT       level=0..1        progress=0..1\n"
            "  battery=-1..100    battery_mv=MV     usb=true|false\n"
            "  charging=true|false                  asleep=true|false\n"
            "  wifi=off|no_network|connecting|connected|failed|not_nearby\n"
            "  ble=off|advertising|connected         passkey=0..999999\n"
            "  paired=true|false  link=boot|unpaired|pairing|confirm|connecting|online|offline|error\n"
            "  speaker=true|false brightness=10..100 advance=MILLISECONDS\n"
            "  pet_mode=MODE pet_x=-100..100 pet_hunger=0..100 pet_happiness=0..100\n"
            "  pet_energy=0..100 pet_object=NAME pet_gesture=NAME pet_timer=TEXT\n"
            "  pet_app=TEXT vision=TEXT\n"
            "  page=face|vision|pet|depth touch_down=X,Y touch_up=X,Y\n"
            "\n"
            "Interactive keys: F1..F7 select face states, H is happy, Space is\n"
            "push-to-talk, +/- change level, [/] change progress, S sleeps,\n"
            "P writes muse-simulator.ppm, Esc quits. Mouse input is touch.\n",
            argv0);
}

static void on_signal(int signum)
{
    (void)signum;
    s_quit = 1;
}

static void render_for(uint32_t duration_ms, bool real_time)
{
    uint32_t elapsed = 0;
    while (!s_quit && elapsed < duration_ms) {
        uint32_t step = duration_ms - elapsed;
        uint32_t max_step = real_time ? 20 : FRAME_STEP_MS;
        if (step > max_step) {
            step = max_step;
        }
        sim_time_advance_us((int64_t)step * 1000);
        if (s_live_state) poll_live_state();
        lv_timer_handler();
        if (real_time) {
            SDL_Delay(step);
        }
        elapsed += step;
    }
}

static bool write_snapshot(const char *path)
{
    lv_display_t *display = sim_board_display();
    if (!display) {
        fprintf(stderr, "simulator display is not ready\n");
        return false;
    }
    lv_refr_now(display);
    SDL_Renderer *renderer = lv_sdl_window_get_renderer(display);
    int width = 0;
    int height = 0;
    if (!renderer || SDL_GetRendererOutputSize(renderer, &width, &height) != 0
        || width <= 0 || height <= 0) {
        fprintf(stderr, "could not read simulator renderer: %s\n", SDL_GetError());
        return false;
    }
    size_t row_bytes = (size_t)width * 3;
    uint8_t *pixels = malloc(row_bytes * (size_t)height);
    if (!pixels) {
        fprintf(stderr, "could not allocate simulator screenshot\n");
        return false;
    }
    if (!sim_damage_read_rgb(pixels, width, height) &&
        SDL_RenderReadPixels(renderer, NULL, SDL_PIXELFORMAT_RGB24, pixels,
                             (int)row_bytes) != 0) {
        fprintf(stderr, "could not capture simulator screen: %s\n", SDL_GetError());
        free(pixels);
        return false;
    }
    FILE *out = fopen(path, "wb");
    if (!out) {
        fprintf(stderr, "%s: %s\n", path, strerror(errno));
        free(pixels);
        return false;
    }
    fprintf(out, "P6\n%d %d\n255\n", width, height);
    if (fwrite(pixels, row_bytes, (size_t)height, out) != (size_t)height) {
        fprintf(stderr, "%s: write failed\n", path);
        fclose(out);
        free(pixels);
        return false;
    }
    bool ok = fclose(out) == 0;
    free(pixels);
    if (ok) {
        fprintf(stderr, "%s: %dx%d\n", path, width, height);
    }
    return ok;
}

static bool parse_bool(const char *text, bool *out)
{
    if (!strcmp(text, "true") || !strcmp(text, "on") || !strcmp(text, "1")) {
        *out = true;
        return true;
    }
    if (!strcmp(text, "false") || !strcmp(text, "off") || !strcmp(text, "0")) {
        *out = false;
        return true;
    }
    return false;
}

static bool parse_long(const char *text, long min, long max, long *out)
{
    char *end = NULL;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno || end == text || *end || value < min || value > max) {
        return false;
    }
    *out = value;
    return true;
}

static bool parse_float(const char *text, float min, float max, float *out)
{
    char *end = NULL;
    errno = 0;
    float value = strtof(text, &end);
    if (errno || end == text || *end || !isfinite(value) || value < min || value > max) {
        return false;
    }
    *out = value;
    return true;
}

static void select_mode(muse_mode_t mode)
{
    /* Production shutdown permits only Idle next. Preview controls should
     * still be able to select any state after showing Off. */
    if (muse_state_mode(NULL) == MUSE_MODE_OFF && mode != MUSE_MODE_OFF) {
        muse_state_set_mode(MUSE_MODE_IDLE);
    }
    muse_state_set_mode(mode);
}

static bool set_face(const char *value)
{
    static const struct {
        const char *name;
        muse_mode_t mode;
    } modes[] = {
        { "boot", MUSE_MODE_BOOT },
        { "idle", MUSE_MODE_IDLE },
        { "listening", MUSE_MODE_LISTENING },
        { "thinking", MUSE_MODE_THINKING },
        { "speaking", MUSE_MODE_SPEAKING },
        { "error", MUSE_MODE_ERROR },
        { "off", MUSE_MODE_OFF },
    };
    if (!strcmp(value, "happy")) {
        select_mode(MUSE_MODE_IDLE);
        muse_state_make_happy();
        return true;
    }
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); i++) {
        if (!strcmp(value, modes[i].name)) {
            select_mode(modes[i].mode);
            return true;
        }
    }
    return false;
}

static bool set_wifi(const char *value)
{
    static const struct {
        const char *name;
        muse_wifi_state_t state;
    } states[] = {
        { "off", MUSE_WIFI_OFF },
        { "no_network", MUSE_WIFI_NO_NETWORK },
        { "connecting", MUSE_WIFI_CONNECTING },
        { "connected", MUSE_WIFI_CONNECTED },
        { "failed", MUSE_WIFI_FAILED },
        { "not_nearby", MUSE_WIFI_NOT_NEARBY },
    };
    for (size_t i = 0; i < sizeof(states) / sizeof(states[0]); i++) {
        if (!strcmp(value, states[i].name)) {
            sim_services_set_wifi(states[i].state,
                                  states[i].state == MUSE_WIFI_CONNECTED ? "Muse Simulator" : "");
            return true;
        }
    }
    return false;
}

static bool set_ble(const char *value)
{
    static const struct {
        const char *name;
        muse_ble_state_t state;
    } states[] = {
        { "off", MUSE_BLE_OFF },
        { "advertising", MUSE_BLE_ADVERTISING },
        { "connected", MUSE_BLE_CONNECTED },
    };
    for (size_t i = 0; i < sizeof(states) / sizeof(states[0]); i++) {
        if (!strcmp(value, states[i].name)) {
            s_ble_state = states[i].state;
            sim_services_set_ble(s_ble_state, s_ble_name, s_passkey);
            return true;
        }
    }
    return false;
}

static bool set_link(const char *value)
{
    static const struct {
        const char *name;
        muse_link_state_t state;
    } states[] = {
        { "boot", MUSE_LINK_BOOT },
        { "unpaired", MUSE_LINK_UNPAIRED },
        { "pairing", MUSE_LINK_PAIRING },
        { "confirm", MUSE_LINK_CONFIRM },
        { "connecting", MUSE_LINK_CONNECTING },
        { "online", MUSE_LINK_ONLINE },
        { "offline", MUSE_LINK_OFFLINE },
        { "error", MUSE_LINK_ERROR },
    };
    for (size_t i = 0; i < sizeof(states) / sizeof(states[0]); i++) {
        if (!strcmp(value, states[i].name)) {
            sim_services_set_link_state(states[i].state);
            return true;
        }
    }
    return false;
}

static bool apply_setting(const char *key, const char *value, bool real_time)
{
    bool flag;
    long number;
    float scalar;
    /* The wake-word service consumes these fields from the same state file. */
    if (!strcmp(key, "call")) {
        return !strcmp(value, "off") || !strcmp(value, "connecting") ||
               !strcmp(value, "active") || !strcmp(value, "ending");
    }
    if (!strcmp(key, "wake_mode")) {
        return !strcmp(value, "note") || !strcmp(value, "call");
    }
    if (!strcmp(key, "page")) {
        if (!strcmp(value, "pet") || !strcmp(value, "vision")) {
            sim_services_show_controls(!strcmp(value, "pet"));
            muse_ui_show_settings();
        }
        else if (!strcmp(value, "face")) muse_ui_show_face();
        else if (!strcmp(value, "depth")) muse_ui_show_depth();
        else return false;
        return true;
    }
    if (!strcmp(key, "touch_down") || !strcmp(key, "touch_up")) {
        int x, y;
        char extra;
        if (sscanf(value, "%d,%d%c", &x, &y, &extra) != 2 ||
            x < 0 || x >= 480 || y < 0 || y >= 320) return false;
        SDL_Window *window = lv_sdl_window_get_window(sim_board_display());
        SDL_Event event = {0};
        event.type = !strcmp(key, "touch_down") ? SDL_MOUSEBUTTONDOWN : SDL_MOUSEBUTTONUP;
        event.button.windowID = SDL_GetWindowID(window);
        event.button.button = SDL_BUTTON_LEFT;
        event.button.x = x;
        event.button.y = y;
        return SDL_PushEvent(&event) == 1;
    }
    if (!strcmp(key, "face")) {
        return set_face(value);
    }
    if (!strcmp(key, "caption")) {
        muse_state_set_caption("%s", value);
        return true;
    }
    if (!strcmp(key, "level") && parse_float(value, 0.0f, 1.0f, &scalar)) {
        s_level = scalar;
        muse_state_set_level(s_level);
        return true;
    }
    if (!strcmp(key, "progress") && parse_float(value, 0.0f, 1.0f, &scalar)) {
        s_progress = scalar;
        muse_state_set_progress(s_progress);
        return true;
    }
    if (!strcmp(key, "battery") && parse_long(value, -1, 100, &number)) {
        s_power.battery_pct = (int)number;
        muse_state_set_power(&s_power);
        return true;
    }
    if (!strcmp(key, "battery_mv") && parse_long(value, 0, 6000, &number)) {
        s_power.battery_mv = (int)number;
        muse_state_set_power(&s_power);
        return true;
    }
    if (!strcmp(key, "usb") && parse_bool(value, &flag)) {
        s_power.usb = flag;
        muse_state_set_power(&s_power);
        return true;
    }
    if (!strcmp(key, "charging") && parse_bool(value, &flag)) {
        s_power.charging = flag;
        muse_state_set_power(&s_power);
        return true;
    }
    if (!strcmp(key, "asleep") && parse_bool(value, &flag)) {
        muse_state_set_asleep(flag);
        return true;
    }
    if (!strcmp(key, "wifi")) {
        return set_wifi(value);
    }
    if (!strcmp(key, "ble")) {
        return set_ble(value);
    }
    if (!strcmp(key, "passkey") && parse_long(value, 0, 999999, &number)) {
        s_passkey = (uint32_t)number;
        sim_services_set_ble(s_ble_state, s_ble_name, s_passkey);
        return true;
    }
    if (!strcmp(key, "paired") && parse_bool(value, &flag)) {
        sim_services_set_paired(flag);
        return true;
    }
    if (!strcmp(key, "link")) {
        return set_link(value);
    }
    if (!strcmp(key, "audio") && parse_bool(value, &flag)) {
        sim_services_set_audio(flag);
        return true;
    }
    if (!strcmp(key, "speaker") && parse_bool(value, &flag)) {
        sim_services_set_speaker(flag);
        return true;
    }
    if (!strcmp(key, "brightness") && parse_long(value, 10, 100, &number)) {
        sim_services_set_brightness((int)number);
        return true;
    }
    if (!strcmp(key, "pet_x")) {
        if (!parse_long(value, -100, 100, &number)) return false;
        muse_state_set_pet_x((int)number);
        return true;
    }
    if (!strncmp(key, "pet_", 4) || !strcmp(key, "vision")) {
        return sim_services_set_pet_value(key, value);
    }
    if (!strcmp(key, "advance") && parse_long(value, 0, 3600000, &number)) {
        render_for((uint32_t)number, real_time);
        return true;
    }
    return false;
}

static char *trim(char *text)
{
    while (isspace((unsigned char)*text)) {
        text++;
    }
    size_t n = strlen(text);
    while (n && isspace((unsigned char)text[n - 1])) {
        text[--n] = '\0';
    }
    return text;
}

/* Test scenarios are strict. A live snapshot is shared with the wake-word
 * service and can contain fields this version of the display doesn't use. */
static bool read_settings(const char *path, bool real_time, bool strict)
{
    FILE *in = fopen(path, "r");
    if (!in) {
        fprintf(stderr, "%s: %s\n", path, strerror(errno));
        return false;
    }
    char line[1024];
    unsigned line_no = 0;
    bool ok = true;
    while (ok && fgets(line, sizeof(line), in)) {
        line_no++;
        char *entry = trim(line);
        if (!*entry || *entry == '#') {
            continue;
        }
        char *equals = strchr(entry, '=');
        if (!equals) {
            if (!strict) continue;
            fprintf(stderr, "%s:%u: expected key=value\n", path, line_no);
            ok = false;
            break;
        }
        *equals = '\0';
        char *key = trim(entry);
        char *value = trim(equals + 1);
        if (!apply_setting(key, value, real_time) && strict) {
            fprintf(stderr, "%s:%u: unsupported or invalid setting: %s=%s\n",
                    path, line_no, key, value);
            ok = false;
        }
    }
    if (ferror(in)) {
        fprintf(stderr, "%s: read failed\n", path);
        ok = false;
    }
    fclose(in);
    return ok;
}

static int event_watch(void *userdata, SDL_Event *event)
{
    sim_damage_event(event);
    (void)userdata;
    if (event->type == SDL_WINDOWEVENT && event->window.event == SDL_WINDOWEVENT_FOCUS_LOST) {
        muse_backend_talk(false);
    }
    if (event->type == SDL_QUIT) {
        muse_backend_talk(false);
        s_quit = 1;
        return 1;
    }
    if (!s_ready || (event->type != SDL_KEYDOWN && event->type != SDL_KEYUP)) {
        return 1;
    }
    bool down = event->type == SDL_KEYDOWN;
    if (down && event->key.repeat) {
        return 1;
    }
    SDL_Keycode key = event->key.keysym.sym;
    if (down && key == SDLK_ESCAPE) {
        s_quit = 1;
    } else if (down && key >= SDLK_F1 && key <= SDLK_F7) {
        static const muse_mode_t modes[] = {
            MUSE_MODE_BOOT, MUSE_MODE_IDLE, MUSE_MODE_LISTENING, MUSE_MODE_THINKING,
            MUSE_MODE_SPEAKING, MUSE_MODE_ERROR, MUSE_MODE_OFF,
        };
        select_mode(modes[key - SDLK_F1]);
    } else if (down && key == SDLK_h) {
        select_mode(MUSE_MODE_IDLE);
        muse_state_make_happy();
    } else if (key == SDLK_SPACE) {
        muse_backend_talk(down);
    } else if (down && (key == SDLK_PLUS || key == SDLK_EQUALS || key == SDLK_KP_PLUS)) {
        s_level = s_level < 0.9f ? s_level + 0.1f : 1.0f;
        muse_state_set_level(s_level);
    } else if (down && (key == SDLK_MINUS || key == SDLK_KP_MINUS)) {
        s_level = s_level > 0.1f ? s_level - 0.1f : 0.0f;
        muse_state_set_level(s_level);
    } else if (down && key == SDLK_RIGHTBRACKET) {
        s_progress = s_progress < 0.9f ? s_progress + 0.1f : 1.0f;
        muse_state_set_progress(s_progress);
    } else if (down && key == SDLK_LEFTBRACKET) {
        s_progress = s_progress > 0.1f ? s_progress - 0.1f : 0.0f;
        muse_state_set_progress(s_progress);
    } else if (down && key == SDLK_m) {
        send_action("mute");
    } else if (down && key == SDLK_s) {
        muse_state_set_asleep(!muse_state_asleep());
    } else if (down && key == SDLK_p) {
        (void)write_snapshot("muse-simulator.ppm");
    }
    return 1;
}

int main(int argc, char **argv)
{
    const char *scenario = NULL;
    const char *screenshot = NULL;
    uint32_t run_ms = 1000;
    bool headless = false;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--headless")) {
            headless = true;
        } else if (!strcmp(argv[i], "--scenario") && i + 1 < argc) {
            scenario = argv[++i];
        } else if (!strcmp(argv[i], "--screenshot") && i + 1 < argc) {
            screenshot = argv[++i];
        } else if (!strcmp(argv[i], "--run-ms") && i + 1 < argc) {
            long value;
            if (!parse_long(argv[++i], 0, 3600000, &value)) {
                fprintf(stderr, "invalid --run-ms value\n");
                return 2;
            }
            run_ms = (uint32_t)value;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage(stdout, argv[0]);
            return 0;
        } else {
            fprintf(stderr, "unknown or incomplete option: %s\n", argv[i]);
            usage(stderr, argv[0]);
            return 2;
        }
    }

    if (headless) {
        (void)setenv("SDL_VIDEODRIVER", "dummy", 1);
        (void)setenv("SDL_AUDIODRIVER", "dummy", 1);
    }
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    sim_time_reset();
    sim_services_reset();
    lv_init();
    muse_state_init();
    muse_state_set_power(&s_power);
    muse_board = sim_board_get();
    if (muse_board->init && muse_board->init() != ESP_OK) {
        fprintf(stderr, "simulator board initialization failed\n");
        return 1;
    }
    if (muse_ui_start() != ESP_OK) {
        fprintf(stderr, "UI initialization failed\n");
        return 1;
    }
    if (headless) {
        /* Dummy SDL desktops have an arbitrary size; capture the board size. */
        SDL_Window *window = lv_sdl_window_get_window(sim_board_display());
        if (window) {
            SDL_SetWindowFullscreen(window, 0);
            SDL_SetWindowSize(window, muse_board->width, muse_board->height);
        }
    }
    /* Scripted previews must not be overwritten by the running backend. */
    s_live_state = !headless && !scenario;
    SDL_AddEventWatch(event_watch, NULL);
    s_ready = true;
    select_mode(MUSE_MODE_BOOT);
    sim_services_set_link_state(MUSE_LINK_CONNECTING);
    sim_services_set_wifi(MUSE_WIFI_CONNECTING, "RDK X5");
    sim_services_set_ble(MUSE_BLE_OFF, "RDK X5", 0);
    muse_state_set_caption("%s", "");

    if (scenario && !read_settings(scenario, !headless, true)) {
        return 2;
    }
    if (headless || screenshot) {
        render_for(run_ms, false);
        if (screenshot && !write_snapshot(screenshot)) {
            return 1;
        }
    } else {
        sim_time_use_realtime(true);
        while (!s_quit) {
            if (s_live_state) poll_live_state();
            uint32_t next = lv_timer_handler();
            /* Sleep until the next timer, retaining responsive touch input.
             * Rendering time already advances the monotonic clock. */
            SDL_Delay(next > 10 ? 10 : (next ? next : 1));
        }
    }

    SDL_DelEventWatch(event_watch, NULL);
    lv_deinit();
    return 0;
}
