/*
 * ui.c:
 *
 * Writes the traffic statistics as JSON to stdout, one object per line,
 * once per RESOLUTION seconds.
 */

#include "config.h"

#include <arpa/inet.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "ui_common.h"
#include "ui.h"
#include "screenfilter.h"
#include "jsondump/jsondump.h"

/*
 * Writes the name of a host (resolved if enabled) into buf.  Returns 0 if
 * the address is unspecified (i.e. aggregated away), 1 otherwise.
 */
static int host_name(char *buf, int af, struct in6_addr *addr) {
    if(IN6_IS_ADDR_UNSPECIFIED(addr)) {
        buf[0] = '\0';
        return 0;
    }
    if(options.dnsresolution) {
        resolve(af, addr, buf, HOSTNAME_LENGTH);
    }
    else {
        inet_ntop(af, addr, buf, HOSTNAME_LENGTH);
    }
    return 1;
}

/*
 * Writes the service name (if known and enabled) or port number into buf.
 */
static void port_name(char *buf, unsigned int port, unsigned int protocol) {
    ip_service skey;
    char* s_name;
    union {
        char **ch_pp;
        void **void_pp;
    } u_s_name = { &s_name };

    skey.port = port;
    skey.protocol = protocol;
    if(options.portresolution && hash_find(service_hash, &skey, u_s_name.void_pp) == HASH_STATUS_OK) {
        snprintf(buf, HOSTNAME_LENGTH, "%s", s_name);
    }
    else {
        snprintf(buf, HOSTNAME_LENGTH, "%u", port);
    }
}

/*
 * Builds the JSON map describing one end of a connection.  "addr" is always
 * the numeric address, "host" the resolved name or null if there is none
 * (resolution disabled, pending or failed).  Both are null if the address
 * was aggregated away.
 */
static struct json_dump *endpoint_json(int af, struct in6_addr *addr, unsigned int port, unsigned int protocol) {
    struct json_dump *jd = jd_create(0);
    char name[HOSTNAME_LENGTH], numeric[INET6_ADDRSTRLEN];

    if(jd == NULL || jd_map_start(jd) != 0) {
        jd_destroy(jd);
        return NULL;
    }
    if(!IN6_IS_ADDR_UNSPECIFIED(addr) && inet_ntop(af, addr, numeric, sizeof numeric) != NULL) {
        jd_map_add_string(jd, "addr", numeric);
        /* The resolver returns the numeric address until a name is known. */
        if(host_name(name, af, addr) && strcmp(name, numeric) != 0) {
            jd_map_add_string(jd, "host", name);
        }
        else {
            jd_map_add_null(jd, "host");
        }
    }
    else {
        jd_map_add_null(jd, "addr");
        jd_map_add_null(jd, "host");
    }
    if(port != 0) {
        jd_map_add_int(jd, "port", port);
        port_name(name, port, protocol);
        jd_map_add_string(jd, "service", name);
    }
    else {
        jd_map_add_null(jd, "port");
    }
    return jd;
}

/* Builds a JSON list of rates (bytes/s) for the averaging intervals. */
static struct json_dump *rates_json(const double long *rates) {
    struct json_dump *jd = jd_create(0);
    int i;

    if(jd == NULL || jd_list_start(jd) != 0) {
        jd_destroy(jd);
        return NULL;
    }
    for(i = 0; i < HISTORY_DIVISIONS; i++) {
        jd_list_add_int64(jd, (int64_t)rates[i]);
    }
    return jd;
}

/* Adds a child to a map and frees it; tolerates a NULL child. */
static void map_add_child_free(struct json_dump *jd, const char *key, struct json_dump *child) {
    if(child == NULL) {
        jd_map_add_null(jd, key);
        return;
    }
    jd_map_add_child(jd, key, child);
    jd_destroy(child);
}

/*
 * Writes the current statistics as a single line of JSON to stdout.
 * Rates are in bytes per second, averaged over the last 2, 10 and 40 seconds.
 */
void ui_print(void) {
    sorted_list_node* nn = NULL;
    struct json_dump *root, *conns, *tot;

    root = jd_create(0);
    conns = jd_create(0);
    tot = jd_create(0);
    if(root == NULL || conns == NULL || tot == NULL) {
        goto out;
    }
    jd_map_start(root);
    jd_list_start(conns);
    jd_map_start(tot);

    while((nn = sorted_list_next_item(&screen_list, nn)) != NULL) {
        host_pair_line* l = (host_pair_line*)nn->data;
        struct json_dump *c = jd_create(0);
        char h1[HOSTNAME_LENGTH], h2[HOSTNAME_LENGTH], sp[HOSTNAME_LENGTH];

        if(c == NULL) {
            continue;
        }

        if(options.screenfilter != NULL) {
            host_name(h1, l->ap.af, &l->ap.src6);
            host_name(h2, l->ap.af, &l->ap.dst6);
            if(l->ap.src_port) {
                port_name(sp, l->ap.src_port, l->ap.protocol);
                snprintf(h1 + strlen(h1), sizeof(h1) - strlen(h1), ":%s", sp);
            }
            if(l->ap.dst_port) {
                port_name(sp, l->ap.dst_port, l->ap.protocol);
                snprintf(h2 + strlen(h2), sizeof(h2) - strlen(h2), ":%s", sp);
            }
            if(!screen_filter_match(h1) && !screen_filter_match(h2)) {
                jd_destroy(c);
                continue;
            }
        }

        jd_map_start(c);
        map_add_child_free(c, "src", endpoint_json(l->ap.af, &l->ap.src6, l->ap.src_port, l->ap.protocol));
        map_add_child_free(c, "dst", endpoint_json(l->ap.af, &l->ap.dst6, l->ap.dst_port, l->ap.protocol));
        jd_map_add_int(c, "protocol", l->ap.protocol);
        map_add_child_free(c, "sent_rate", rates_json(l->sent));
        map_add_child_free(c, "recv_rate", rates_json(l->recv));
        jd_map_add_int64(c, "total_sent", (int64_t)l->total_sent);
        jd_map_add_int64(c, "total_recv", (int64_t)l->total_recv);
        jd_list_add_child(conns, c);
        jd_destroy(c);
    }

    jd_map_add_int64(root, "timestamp", (int64_t)time(NULL));
    jd_map_add_string(root, "interface", options.interface);
    jd_map_add_child(root, "connections", conns);

    map_add_child_free(tot, "sent_rate", rates_json(totals.sent));
    map_add_child_free(tot, "recv_rate", rates_json(totals.recv));
    jd_map_add_int64(tot, "total_sent", (int64_t)history_totals.total_sent);
    jd_map_add_int64(tot, "total_recv", (int64_t)history_totals.total_recv);
    jd_map_add_int64(tot, "peak_sent_rate", peaksent / RESOLUTION);
    jd_map_add_int64(tot, "peak_recv_rate", peakrecv / RESOLUTION);
    jd_map_add_int64(tot, "peak_total_rate", peaktotal / RESOLUTION);
    jd_map_add_child(root, "totals", tot);

    printf("%s\n", root->buf);
    fflush(stdout);

out:
    jd_destroy(root);
    jd_destroy(conns);
    jd_destroy(tot);
}

void ui_tick(int print) {
  if(print) {
    ui_print();
  }
}

void ui_init(void) {
    screen_list_init();
    screen_hash = addr_hash_create();

    service_hash = serv_hash_create();
    serv_hash_initialise(service_hash);
}

/* All the work is done from tick(), driven by the packet thread. */
void ui_loop(void) {
    extern sig_atomic_t foad;

    while(foad == 0) {
        tick(0);
        usleep(200000);
    }
}

void ui_finish(void) {
}
