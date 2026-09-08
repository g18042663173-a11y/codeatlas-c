/* CC0-1.0: bounded-buffer parsing observations. */
#include <assert.h>
#include <stdio.h>
#include "cJSON.h"
int main(void) {
    const char text[]="{\"n\":7} trailing", bounded[]={'[','1',']'};
    const char *end=NULL;
    cJSON *value=cJSON_ParseWithLengthOpts(text,sizeof(text),&end,0);
    assert(value && end==text+7);
    printf("prefix input=%s accepted=1 end_offset=%td suffix=%s\n",text,end-text,end);
    cJSON_Delete(value);
    value=cJSON_ParseWithLengthOpts(text,sizeof(text),&end,1);
    assert(value==NULL);
    printf("require_terminator accepted=0 end_offset=%td\n",end-text);
    value=cJSON_ParseWithLengthOpts(bounded,sizeof(bounded),&end,0);
    assert(value && end==bounded+3);
    printf("three_bytes_no_NUL accepted=1 end_offset=%td\n",end-bounded);
    cJSON_Delete(value);
    value=cJSON_ParseWithLengthOpts(bounded,sizeof(bounded),&end,1);
    assert(value==NULL);
    printf("three_bytes_require_NUL accepted=0 end_offset=%td\n",end-bounded);
    return 0;
}
