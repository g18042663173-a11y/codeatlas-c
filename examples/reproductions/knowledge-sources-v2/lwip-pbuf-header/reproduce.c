/* CC0-1.0: borrowed storage remains caller-owned. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/pbuf.h"
int main(void) {
    char storage[]="HEADpayload";struct pbuf p;memset(&p,0,sizeof(p));
    p.payload=storage+4;p.len=p.tot_len=7;p.ref=1;p.type_internal=PBUF_REF;
    int result=pbuf_header(&p,4);
    assert(result!=0 && p.payload==storage+4 && p.len==7);
    printf("REF add4 result=%d offset=%td len=%u total=%u\n",result,(char *)p.payload-storage,p.len,p.tot_len);
    result=pbuf_header(&p,-2);assert(result==0 && p.payload==storage+6 && p.len==5);
    printf("REF remove2 result=%d offset=%td len=%u total=%u\n",result,(char *)p.payload-storage,p.len,p.tot_len);
    result=pbuf_header(&p,-6);assert(result!=0 && p.payload==storage+6 && p.len==5);
    printf("REF remove6 result=%d offset=%td len=%u total=%u\n",result,(char *)p.payload-storage,p.len,p.tot_len);
    result=pbuf_header_force(&p,6);assert(result==0 && p.payload==storage && p.len==11);
    printf("REF force_add6 valid_reserved_storage result=%d offset=%td len=%u total=%u\n",result,(char *)p.payload-storage,p.len,p.tot_len);
    return 0;
}
