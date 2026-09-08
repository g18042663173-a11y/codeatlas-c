/* CC0-1.0: stack-backed pbufs; inspect links/refcounts, never free stack storage. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/pbuf.h"
static void init(struct pbuf *p,void *bytes,u16_t length) {
    memset(p,0,sizeof(*p));p->payload=bytes;p->len=p->tot_len=length;
    p->ref=1;p->type_internal=PBUF_REF;
}
int main(void) {
    char a[]="ab",b[]="cde",c[]="fghi";struct pbuf first,middle,last;
    init(&first,a,2);init(&middle,b,3);init(&last,c,4);
    pbuf_cat(&first,&middle);assert(first.next==&middle && first.tot_len==5 && middle.ref==1);
    printf("cat first_total=%u tail_ref=%u tail_total=%u\n",first.tot_len,middle.ref,middle.tot_len);
    pbuf_chain(&first,&last);assert(middle.next==&last && first.tot_len==9 && middle.tot_len==7 && last.ref==2);
    printf("chain first_total=%u middle_total=%u tail_ref=%u tail_total=%u\n",first.tot_len,middle.tot_len,last.ref,last.tot_len);
    pbuf_ref(&last);assert(last.ref==3 && first.ref==1 && middle.ref==1);
    printf("explicit_ref tail_ref=%u first_ref=%u middle_ref=%u\n",last.ref,first.ref,middle.ref);
    printf("lifetime=caller_owned_stack no_pbuf_free_called\n");
    return 0;
}
