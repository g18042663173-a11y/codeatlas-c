/* CC0-1.0: object/array/type comparison matrix. */
#include <assert.h>
#include <stdio.h>
#include "cJSON.h"
struct vector { const char *left,*right; int sensitive,equal; };
int main(void) {
    const struct vector cases[]={
        {"{\"a\":1,\"b\":2}","{\"b\":2,\"a\":1}",1,1},
        {"[1,2]","[2,1]",1,0},
        {"{\"Name\":\"x\"}","{\"name\":\"x\"}",0,1},
        {"{\"Name\":\"x\"}","{\"name\":\"x\"}",1,0},
        {"{\"k\":\"X\"}","{\"k\":\"x\"}",0,0},
        {"{\"a\":1}","{\"a\":1,\"b\":2}",1,0},
        {"{\"a\":1,\"b\":2}","{\"a\":1}",1,0},
        {"1","\"1\"",0,0},
        {"{\"items\":[true,null,{\"x\":3}]}","{\"items\":[true,null,{\"x\":3}]}",1,1},
        {"{\"items\":[true,null,{\"x\":3}]}","{\"items\":[true,null,{\"x\":4}]}",1,0}
    };
    for(unsigned i=0;i<sizeof(cases)/sizeof(cases[0]);++i) {
        cJSON *a=cJSON_Parse(cases[i].left), *b=cJSON_Parse(cases[i].right);assert(a && b);
        int equal=cJSON_Compare(a,b,cases[i].sensitive);
        printf("vector=%u sensitive=%d left=%s right=%s equal=%d\n",i+1,cases[i].sensitive,cases[i].left,cases[i].right,equal);
        assert(equal==cases[i].equal);cJSON_Delete(a);cJSON_Delete(b);
    }
    return 0;
}
