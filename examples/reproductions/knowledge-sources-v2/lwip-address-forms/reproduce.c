/* CC0-1.0: real parser with the pinned unit-test configuration. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "lwip/ip4_addr.h"
int main(void) {
    const char *inputs[]={"127.1","0x7f.1","0177.0.0.1","127.0.0.1","1.2.65535","256.1.1.1","1.2.3.256","08.0.0.1","1.2.3.4x"};
    const char *expected[]={"127.0.0.1","127.0.0.1","127.0.0.1","127.0.0.1","1.2.255.255",NULL,NULL,NULL,NULL};
    for(unsigned i=0;i<sizeof(inputs)/sizeof(inputs[0]);++i) {
        ip4_addr_t address;char text[IP4ADDR_STRLEN_MAX];
        int ok=ip4addr_aton(inputs[i],&address);assert(ok==(expected[i]!=NULL));
        if(ok) {assert(ip4addr_ntoa_r(&address,text,sizeof(text)));assert(strcmp(text,expected[i])==0);}
        printf("input=%s accepted=%d normalized=%s\n",inputs[i],ok,ok?text:"not_available");
    }
    return 0;
}
