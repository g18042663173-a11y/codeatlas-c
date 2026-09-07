#include <stdio.h>
#include <string.h>

#include "lwip/netif.h"

static err_t init_ok(struct netif *netif)
{
    const char *name = (const char *)netif->state;
    netif->name[0] = name[0];
    netif->name[1] = name[1];
    return ERR_OK;
}

static err_t init_fail(struct netif *netif)
{
    LWIP_UNUSED_ARG(netif);
    return ERR_IF;
}

static err_t input_ok(struct pbuf *packet, struct netif *netif)
{
    LWIP_UNUSED_ARG(packet);
    LWIP_UNUSED_ARG(netif);
    return ERR_OK;
}

static char state_first[] = "ca";
static char state_second[] = "cb";
static char state_failed[] = "cc";

int main(void)
{
    struct netif first;
    struct netif second;
    struct netif failed;
    struct netif flags;
    struct netif *first_result;
    struct netif *second_result;
    struct netif *failed_result;
    u8_t client_first;
    u8_t client_second;
    memset(&first, 0, sizeof(first));
    memset(&second, 0, sizeof(second));
    memset(&failed, 0, sizeof(failed));
    memset(&flags, 0, sizeof(flags));
    first.flags = NETIF_FLAG_UP;
    first.mtu = 999;

    printf("initial_default=%s\n", netif_default == NULL ? "none" : "set");
    first_result = netif_add_noaddr(&first, state_first, init_ok, input_ok);
    printf("add_first=%s\n", first_result == &first ? "ok" : "fail");
    printf("first_flags=%s\n", netif_is_up(&first) ? "up" : "down");
    printf("first_mtu=%u\n", (unsigned int)first.mtu);
    printf("first_state=%s\n", first.state == state_first ? "stored" : "other");
    printf("index_one=%s\n", netif_get_by_index(1) == &first ? "first" : "none");
    printf("default_after_add=%s\n", netif_default == &first ? "first" : "none");

    netif_set_default(&first);
    printf("default_after_explicit=%s\n", netif_default == &first ? "first" : "other");
    second_result = netif_add_noaddr(&second, state_second, init_ok, input_ok);
    printf("add_second=%s\n", second_result == &second ? "ok" : "fail");
    printf("index_two=%s\n", netif_get_by_index(2) == &second ? "second" : "none");
    printf("default_after_second=%s\n",
           netif_default == &second ? "second" : netif_default == &first ? "first" : "other");

    failed_result = netif_add_noaddr(&failed, state_failed, init_fail, input_ok);
    printf("failed_init_return=%s\n", failed_result == NULL ? "null" : "netif");
    printf("failed_init_index=%s\n", netif_get_by_index(3) == &failed ? "present" : "absent");
    netif_set_default(NULL);

    netif_set_up(&flags);
    printf("admin_up=%s\n", netif_is_up(&flags) ? "yes" : "no");
    netif_set_down(&flags);
    printf("admin_down=%s\n", netif_is_up(&flags) ? "no" : "yes");
    netif_set_link_up(&flags);
    printf("link_up=%s\n", netif_is_link_up(&flags) ? "yes" : "no");
    netif_set_link_down(&flags);

    netif_set_hostname(&flags, "fixture-host");
    printf("hostname=%s\n", netif_get_hostname(&flags) == NULL ? "none" : netif_get_hostname(&flags));
    client_first = netif_alloc_client_data_id();
    client_second = netif_alloc_client_data_id();
    printf("client_ids=%u,%u\n", (unsigned int)client_first, (unsigned int)client_second);

    if (failed_result == &failed) {
        netif_remove(&failed);
    }
    if (second_result == &second) {
        netif_remove(&second);
    }
    if (first_result == &first) {
        netif_remove(&first);
    }
    return 0;
}
