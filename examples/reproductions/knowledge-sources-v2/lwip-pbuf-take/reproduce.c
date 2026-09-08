/* CC0-1.0: bounded writes across packet fragments. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/pbuf.h"
static void show(struct pbuf *p,const char *label,err_t result) {
    char out[10];u16_t n=pbuf_copy_partial(p,out,9,0);out[n]='\0';
    printf("%s result=%d packet=%s total=%u\n",label,result,out,p->tot_len);
}
int main(void) {
    char a[]="abc",b[]="de",c[]="fghi";struct pbuf p[3];memset(p,0,sizeof(p));
    p[0].payload=a;p[0].len=3;p[0].tot_len=9;p[0].next=&p[1];
    p[1].payload=b;p[1].len=2;p[1].tot_len=6;p[1].next=&p[2];
    p[2].payload=c;p[2].len=p[2].tot_len=4;
    err_t result=pbuf_take_at(p,"WXYZ",4,2);
    assert(result==ERR_OK && memcmp(a,"abW",3)==0 && memcmp(b,"XY",2)==0 && memcmp(c,"Zghi",4)==0);
    show(p,"write offset=2 len=4 bytes=WXYZ",result);
    result=pbuf_take_at(p,"NO",2,8);assert(result==ERR_MEM && memcmp(c,"Zghi",4)==0);
    show(p,"oversize offset=8 len=2 unchanged",result);
    result=pbuf_take_at(p,"END",3,6);assert(result==ERR_OK && memcmp(c,"ZEND",4)==0);
    show(p,"exact_end offset=6 len=3 bytes=END",result);
    result=pbuf_take_at(p,"!",1,9);assert(result==ERR_MEM);show(p,"offset_at_end len=1 unchanged",result);
    return 0;
}
