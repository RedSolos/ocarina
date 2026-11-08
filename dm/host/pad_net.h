// Network gamepad (front_mac.m, DM_PAD_UDP=<port>[@<address>]): a pad on another machine, such as a Steam Deck streaming the Mac through Steam Link (whose Steam Input cannot make
// a gamepad on macOS: the Deck's controls arrive as keyboard and mouse), sends its state in small UDP datagrams and the host plays it as one more controller, sticks analog.
// Pure POSIX, so tests/t_pad_net.c checks it over loopback without the Mac front end; scripts/deck_pad_bridge.py is the sender.
//
// Datagram, 20 bytes, little endian (a longer one is read as far as it goes, so a later version can add to the end):
//    0  "NXPD"            magic
//    4  u8 version = 1
//    5  u8 reserved = 0
//    6  u16 sequence      counts up by one per datagram and wraps; an older or repeated one is dropped (UDP may reorder)
//    8  u32 buttons       bits 0..15 = nn::hid::NpadButton, as the DM_BTN_* in dm_input.h (A B X Y LS RS L R ZL ZR + - Left Up Right Down); the other bits are ignored
//   12  s16 lx, ly, rx, ry  the sticks, ±32767; up is positive, as everywhere in dm_input.h
// One sender plays at a time: the first datagram's address is the sender until it has been silent for PAD_NET_TIMEOUT_MS, then the pad is released (nothing stays pressed)
// and the next datagram from any address takes over. The sender keeps talking while nothing changes (a few datagrams a second), which is how a held button survives.
// A datagram from another address meanwhile is dropped. The port is open to the whole network unless an address is given (DM_PAD_UDP=9777@192.168.1.20 binds only that one).
#pragma once
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "dm_input.h"

#define PAD_NET_MAGIC "NXPD"
#define PAD_NET_VERSION 1
#define PAD_NET_SIZE 20
#define PAD_NET_TIMEOUT_MS 500

typedef struct { uint64_t buttons; int32_t lx, ly, rx, ry; } pad_net_state;

static uint16_t pad_net_u16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static int32_t pad_net_axis(const uint8_t *p) {
    int32_t v = (int16_t)pad_net_u16(p);
    return v < -DM_STICK_MAX ? -DM_STICK_MAX : v;   // -32768 is one past what the game's sticks take
}
// A datagram into a state and its sequence number; 0 when it is not ours (wrong magic, a version this host does not know, too short)
static int pad_net_decode(const uint8_t *b, size_t n, pad_net_state *st, uint16_t *seq) {
    if (n < PAD_NET_SIZE || memcmp(b, PAD_NET_MAGIC, 4) || b[4] != PAD_NET_VERSION) return 0;
    *seq = pad_net_u16(b + 6);
    st->buttons = (uint32_t)(b[8] | b[9] << 8 | (uint32_t)b[10] << 16 | (uint32_t)b[11] << 24) & 0xFFFFu;
    st->lx = pad_net_axis(b + 12); st->ly = pad_net_axis(b + 14); st->rx = pad_net_axis(b + 16); st->ry = pad_net_axis(b + 18);
    return 1;
}
// The wire form of a state (the sender's side, for the tests)
static void pad_net_encode(uint8_t *b, const pad_net_state *st, uint16_t seq) {
    memcpy(b, PAD_NET_MAGIC, 4); b[4] = PAD_NET_VERSION; b[5] = 0; b[6] = (uint8_t)seq; b[7] = (uint8_t)(seq >> 8);
    uint32_t m = (uint32_t)st->buttons & 0xFFFFu;
    for (int i = 0; i < 4; i++) b[8 + i] = (uint8_t)(m >> (8 * i));
    const int32_t a[4] = { st->lx, st->ly, st->rx, st->ry };
    for (int i = 0; i < 4; i++) { uint16_t v = (uint16_t)(int16_t)a[i]; b[12 + 2 * i] = (uint8_t)v; b[13 + 2 * i] = (uint8_t)(v >> 8); }
}

// connected 1: a new state from the sender; 0: the sender went silent (st is all zero). Called on the receiver's thread
typedef void (*pad_net_fn)(void *user, int connected, const pad_net_state *st);
typedef struct {
    int fd; pthread_t th; atomic_int stop; pad_net_fn fn; void *user;
    int port;   // the port bound (the test asks for 0 and reads the one it got)
} pad_net;

static uint64_t pad_net_now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000; }
static int pad_net_newer(uint16_t seq, uint16_t last) { return (int16_t)(uint16_t)(seq - last) > 0; }   // modulo 2^16: half the range ahead is newer

static void *pad_net_main(void *arg) {
    pad_net *n = arg;
    int latched = 0, have_seq = 0; uint32_t from_addr = 0; uint16_t last_seq = 0; uint64_t last_rx = 0;
    uint8_t buf[256];
    while (!atomic_load(&n->stop)) {
        struct sockaddr_in from; socklen_t fl = sizeof from;
        ssize_t r = recvfrom(n->fd, buf, sizeof buf, 0, (struct sockaddr *)&from, &fl);   // SO_RCVTIMEO: wakes every 100 ms for the silence check
        uint64_t now = pad_net_now_ms();
        if (latched && now - last_rx > PAD_NET_TIMEOUT_MS) {
            latched = 0; have_seq = 0;
            pad_net_state z = {0}; n->fn(n->user, 0, &z);
        }
        pad_net_state st; uint16_t seq;
        if (r <= 0 || !pad_net_decode(buf, (size_t)r, &st, &seq)) continue;
        if (!latched) { latched = 1; have_seq = 0; from_addr = from.sin_addr.s_addr; }
        else if (from.sin_addr.s_addr != from_addr) continue;   // someone else's datagram while the sender is talking
        if (have_seq && !pad_net_newer(seq, last_seq)) continue;
        last_seq = seq; have_seq = 1; last_rx = now;
        n->fn(n->user, 1, &st);
    }
    return NULL;
}
// spec: "<port>" or "<port>@<ipv4 address>" (port 0: any free one, then n->port tells). 0 when listening, -1 with errno set otherwise
static int pad_net_open(pad_net *n, const char *spec, pad_net_fn fn, void *user) {
    memset(n, 0, sizeof *n); n->fd = -1; n->fn = fn; n->user = user;
    char *end; long port = strtol(spec, &end, 10);
    if (end == spec || port < 0 || port > 65535 || (*end && *end != '@')) { errno = EINVAL; return -1; }
    struct sockaddr_in a; memset(&a, 0, sizeof a); a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); a.sin_addr.s_addr = htonl(INADDR_ANY);
    if (*end == '@' && inet_pton(AF_INET, end + 1, &a.sin_addr) != 1) { errno = EINVAL; return -1; }
    int fd = socket(AF_INET, SOCK_DGRAM, 0); if (fd < 0) return -1;
    struct timeval tv = { 0, 100000 }; setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    int one = 1; setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    socklen_t al = sizeof a;
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || getsockname(fd, (struct sockaddr *)&a, &al) < 0) { int e = errno; close(fd); errno = e; return -1; }
    n->fd = fd; n->port = ntohs(a.sin_port);
    if (pthread_create(&n->th, NULL, pad_net_main, n)) { int e = errno; close(fd); n->fd = -1; errno = e; return -1; }
    return 0;
}
static void pad_net_close(pad_net *n) {
    if (n->fd < 0) return;
    atomic_store(&n->stop, 1); pthread_join(n->th, NULL); close(n->fd); n->fd = -1;
}
