/* CC0-1.0: copy/zero-copy boundaries over a three-fragment packet. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/pbuf.h"
int main(void) {
    char a[]="abc",b[]="de",c[]="fghi",out[16];struct pbuf p[3];memset(p,0,sizeof(p));
    p[0].payload=a;p[0].len=3;p[0].tot_len=9;p[0].next=&p[1];
    p[1].payload=b;p[1].len=2;p[1].tot_len=6;p[1].next=&p[2];
    p[2].payload=c;p[2].len=p[2].tot_len=4;
    u16_t copied=pbuf_copy_partial(p,out,5,2);out[copied]='\0';assert(copied==5 && strcmp(out,"cdefg")==0);
    printf("copy offset=2 requested=5 copied=%u text=%s\n",copied,out);
    copied=pbuf_copy_partial(p,out,9,7);out[copied]='\0';assert(copied==2 && strcmp(out,"hi")==0);
    printf("copy offset=7 requested=9 copied=%u text=%s\n",copied,out);
    void *view=pbuf_get_contiguous(p,NULL,0,2,3);assert(view==b);
    printf("contiguous offset=3 len=2 zero_copy=%d\n",view==b);
    assert(pbuf_get_contiguous(p,NULL,0,4,2)==NULL);printf("cross_fragment without_scratch=null\n");
    view=pbuf_get_contiguous(p,out,sizeof(out),4,2);assert(view==out && memcmp(out,"cdef",4)==0);
    printf("cross_fragment with_scratch copied=%d text=%.*s\n",view==out,4,out);
    assert(pbuf_get_contiguous(p,out,sizeof(out),4,8)==NULL);printf("beyond_packet offset=8 len=4 result=null\n");
    return 0;
}
