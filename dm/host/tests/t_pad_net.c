// Network gamepad (pad_net.h, DM_PAD_UDP): the datagram's decode and encode, the sequence order, the sender latch and the silence release, over loopback
#include <stdio.h>
#include <stdatomic.h>
#include "../pad_net.h"

static int checks, fails;
#define CHECK(c) do { checks++; if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static int calls, last_connected; static pad_net_state last;
static void on_state(void *user, int connected, const pad_net_state *st) {
    (void)user; pthread_mutex_lock(&mu); calls++; last_connected = connected; last = *st; pthread_mutex_unlock(&mu);
}
static int wait_calls(int n, int ms) {   // until the receiver thread has made n calls in all
    for (int i = 0; i < ms; i++) { pthread_mutex_lock(&mu); int c = calls; pthread_mutex_unlock(&mu); if (c >= n) return 1; usleep(1000); }
    return 0;
}
static void send_to(int fd, int port, const void *b, size_t n) {
    struct sockaddr_in a = {0}; a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    sendto(fd, b, n, 0, (struct sockaddr *)&a, sizeof a);
}

int main(void) {
    uint8_t b[PAD_NET_SIZE]; pad_net_state s = {0}, o; uint16_t seq;

    // the wire form: round trip, the byte layout, the clamps
    s.buttons = DM_BTN_A | DM_BTN_ZR | DM_BTN_DOWN; s.lx = 32767; s.ly = -32767; s.rx = 1234; s.ry = -1;
    pad_net_encode(b, &s, 0xBEEF);
    CHECK(!memcmp(b, "NXPD", 4) && b[4] == 1 && b[5] == 0 && b[6] == 0xEF && b[7] == 0xBE);
    CHECK(b[8] == 0x01 && b[9] == 0x82 && b[10] == 0 && b[11] == 0);   // A = bit 0; ZR = bit 9, Down = bit 15
    CHECK(b[12] == 0xFF && b[13] == 0x7F);                              // 32767 little endian
    CHECK(pad_net_decode(b, sizeof b, &o, &seq) && seq == 0xBEEF);
    CHECK(o.buttons == s.buttons && o.lx == 32767 && o.ly == -32767 && o.rx == 1234 && o.ry == -1);
    b[12] = 0x00; b[13] = 0x80; CHECK(pad_net_decode(b, sizeof b, &o, &seq) && o.lx == -32767);   // -32768 is one past the game's sticks
    pad_net_encode(b, &s, 1); b[11] = 0xFF; b[9] = 0xFF; CHECK(pad_net_decode(b, sizeof b, &o, &seq) && (o.buttons & ~0xFFFFull) == 0);   // only the 16 NpadButton bits
    uint8_t big[PAD_NET_SIZE + 8]; pad_net_encode(big, &s, 7); memset(big + PAD_NET_SIZE, 0xAA, 8);
    CHECK(pad_net_decode(big, sizeof big, &o, &seq) && seq == 7);       // a longer datagram is read as far as it goes
    CHECK(!pad_net_decode(b, PAD_NET_SIZE - 1, &o, &seq));              // too short
    pad_net_encode(b, &s, 1); b[0] = 'X'; CHECK(!pad_net_decode(b, sizeof b, &o, &seq));   // not ours
    pad_net_encode(b, &s, 1); b[4] = 2; CHECK(!pad_net_decode(b, sizeof b, &o, &seq));     // a version this host does not know

    // the sequence order wraps
    CHECK(pad_net_newer(2, 1) && !pad_net_newer(1, 1) && !pad_net_newer(1, 2) && pad_net_newer(0, 0xFFFF) && !pad_net_newer(0xFFFF, 0) && pad_net_newer(0x8000, 1));

    // over loopback
    CHECK(pad_net_open(&(pad_net){0}, "70000", on_state, NULL) < 0 && pad_net_open(&(pad_net){0}, "abc", on_state, NULL) < 0 && pad_net_open(&(pad_net){0}, "0@nope", on_state, NULL) < 0);
    pad_net n; CHECK(pad_net_open(&n, "0@127.0.0.1", on_state, NULL) == 0 && n.port > 0);
    int tx = socket(AF_INET, SOCK_DGRAM, 0), tx2 = socket(AF_INET, SOCK_DGRAM, 0);

    s.buttons = DM_BTN_B; s.lx = 100; s.ly = 200; s.rx = -300; s.ry = -400;
    pad_net_encode(b, &s, 10); send_to(tx, n.port, b, sizeof b);
    CHECK(wait_calls(1, 1000));
    pthread_mutex_lock(&mu); CHECK(last_connected == 1 && last.buttons == DM_BTN_B && last.lx == 100 && last.ly == 200 && last.rx == -300 && last.ry == -400); pthread_mutex_unlock(&mu);

    s.buttons = DM_BTN_X; pad_net_encode(b, &s, 9); send_to(tx, n.port, b, sizeof b);      // older: dropped
    pad_net_encode(b, &s, 10); send_to(tx, n.port, b, sizeof b);                           // repeated: dropped
    s.buttons = DM_BTN_Y; pad_net_encode(b, &s, 11); send_to(tx, n.port, b, sizeof b);     // newer: played
    CHECK(wait_calls(2, 1000)); usleep(50000);
    pthread_mutex_lock(&mu); CHECK(calls == 2 && last.buttons == DM_BTN_Y); pthread_mutex_unlock(&mu);

    // garbage from another socket is ignored (the latch is by address, and loopback is one address, so a second socket of ours is not a second sender)
    send_to(tx2, n.port, "hello", 5); usleep(50000);
    pthread_mutex_lock(&mu); CHECK(calls == 2); pthread_mutex_unlock(&mu);

    // silence: the pad is released once, with every control back to zero
    CHECK(wait_calls(3, PAD_NET_TIMEOUT_MS + 1500));
    pthread_mutex_lock(&mu); CHECK(calls == 3 && last_connected == 0 && last.buttons == 0 && !last.lx && !last.ly && !last.rx && !last.ry); pthread_mutex_unlock(&mu);
    usleep(300000); pthread_mutex_lock(&mu); CHECK(calls == 3); pthread_mutex_unlock(&mu);   // and not again

    // the next datagram takes over, even with a lower sequence number than before the silence
    s.buttons = DM_BTN_ZL; pad_net_encode(b, &s, 3); send_to(tx2, n.port, b, sizeof b);
    CHECK(wait_calls(4, 1000));
    pthread_mutex_lock(&mu); CHECK(last_connected == 1 && last.buttons == DM_BTN_ZL); pthread_mutex_unlock(&mu);

    pad_net_close(&n); close(tx); close(tx2);
    printf("t_pad_net: %d checks, %d failed\n", checks, fails);
    return fails ? 1 : 0;
}
