/* CC0-1.0: owned and reference string mutation. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
int main(void) {
    cJSON *owned=cJSON_CreateString("abcdef");
    cJSON *reference=cJSON_CreateStringReference("external");
    cJSON *number=cJSON_CreateNumber(4);
    assert(owned && reference && number);
    char *original=owned->valuestring;
    char *result=cJSON_SetValuestring(owned,"xy");
    assert(result==original && strcmp(result,"xy")==0);
    printf("owned shorten=xy reused_storage=%d\n",result==original);
    result=cJSON_SetValuestring(owned,"a much longer replacement");
    assert(result && strcmp(result,"a much longer replacement")==0);
    printf("owned grow=%s\n",result);
    assert(cJSON_SetValuestring(reference,"changed")==NULL);
    assert(strcmp(reference->valuestring,"external")==0);
    printf("reference rejected=1 preserved=%s\n",reference->valuestring);
    assert(cJSON_SetValuestring(number,"string")==NULL);
    printf("number rejected=1 value=%.0f\n",number->valuedouble);
    cJSON_Delete(owned);cJSON_Delete(reference);cJSON_Delete(number);
    return 0;
}
