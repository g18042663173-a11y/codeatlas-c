#ifndef CODEATLAS_LWIPOPTS_H
#define CODEATLAS_LWIPOPTS_H

/* Minimal, deterministic configuration for the public netif reproduction. */
#define NO_SYS 1
#define SYS_LIGHTWEIGHT_PROT 0
#define MEM_LIBC_MALLOC 1

#define LWIP_IPV4 1
#define LWIP_IPV6 0
#define LWIP_ARP 0
#define LWIP_ETHERNET 0
#define LWIP_RAW 0
#define LWIP_UDP 0
#define LWIP_TCP 0
#define LWIP_DHCP 0
#define LWIP_AUTOIP 0
#define LWIP_IGMP 0
#define LWIP_DNS 0

#define LWIP_NETCONN 0
#define LWIP_SOCKET 0
#define LWIP_STATS 0
#define LWIP_TIMERS 0

#endif
