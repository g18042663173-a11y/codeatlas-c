/* CC0-1.0: byte-order independent checksums; no network traffic. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/inet_chksum.h"
#include "lwip/pbuf.h"
static unsigned display(u16_t value) {const unsigned char *p=(const unsigned char *)&value;return ((unsigned)p[0]<<8)|p[1];}
int main(void) {
    unsigned char bytes[]={0x00,0x01,0xf2,0x03,0xf4,0xf5,0xf6,0xf7,0x11};
    const unsigned splits[][3]={{1,2,6},{3,3,3},{4,4,1},{2,2,5}};
    const u16_t contiguous=inet_chksum(bytes,sizeof(bytes));
    printf("input_hex=0001f203f4f5f6f711 contiguous_checksum=%04x\n",display(contiguous));
    for(unsigned i=0;i<sizeof(splits)/sizeof(splits[0]);++i) {
        struct pbuf p[3];memset(p,0,sizeof(p));unsigned offset=0;
        for(unsigned j=0;j<3;++j) {
            p[j].payload=bytes+offset;p[j].len=(u16_t)splits[i][j];p[j].tot_len=(u16_t)(sizeof(bytes)-offset);
            p[j].next=j<2?&p[j+1]:NULL;offset+=splits[i][j];
        }
        u16_t chained=inet_chksum_pbuf(p);assert(chained==contiguous);
        printf("split=%u+%u+%u checksum=%04x equal_contiguous=1\n",splits[i][0],splits[i][1],splits[i][2],display(chained));
    }
    unsigned char unaligned[sizeof(bytes)+1];memcpy(unaligned+1,bytes,sizeof(bytes));
    u16_t check=inet_chksum(unaligned+1,sizeof(bytes));assert(check==contiguous);
    printf("unaligned_same_bytes checksum=%04x equal_contiguous=1\n",display(check));
    const u16_t even=inet_chksum(bytes,8);assert(display(even)==0x220d && display(contiguous)==0x110d);
    printf("prefix_len=8 checksum=%04x full_len=9 checksum=%04x\n",display(even),display(contiguous));
    return 0;
}
