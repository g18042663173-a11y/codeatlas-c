#include <stdio.h>
#include <string.h>

#include "lwip/netif.h"

static err_t test_init(struct netif *netif)
{
    netif->name[0] = 'c';
    netif->name[1] = 'a';
    netif->mtu = 1500;
    return ERR_OK;
}

static err_t test_input(struct pbuf *packet, struct netif *netif)
{
    LWIP_UNUSED_ARG(packet);
    LWIP_UNUSED_ARG(netif);
    return ERR_OK;
}

static const char *which(const struct netif *value,
                         const struct netif *first,
                         const struct netif *second)
{
    if (value == NULL) {
        return "none";
    }
    if (value == first) {
        return "first";
    }
    if (value == second) {
        return "second";
    }
    return "other";
}

int main(void)
{
    struct netif first;
    struct netif second;
    memset(&first, 0, sizeof(first));
    memset(&second, 0, sizeof(second));

    printf("initial list=%s default=%s\n",
           which(netif_list, &first, &second), which(netif_default, &first, &second));
    printf("add_first=%s\n",
           netif_add_noaddr(&first, NULL, test_init, test_input) == &first ? "ok" : "failed");
    printf("after_first list=%s default=%s\n",
           which(netif_list, &first, &second), which(netif_default, &first, &second));
    netif_set_default(&first);
    printf("after_set_default list=%s default=%s\n",
           which(netif_list, &first, &second), which(netif_default, &first, &second));
    printf("add_second=%s\n",
           netif_add_noaddr(&second, NULL, test_init, test_input) == &second ? "ok" : "failed");
    printf("after_second list=%s default=%s\n",
           which(netif_list, &first, &second), which(netif_default, &first, &second));
    netif_remove(&second);
    netif_remove(&first);
    return 0;
}
