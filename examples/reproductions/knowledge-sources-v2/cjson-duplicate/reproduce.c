/* CC0-1.0: shallow/deep copies and reference conversion. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
int main(void) {
    cJSON *source=cJSON_Parse("{\"nested\":{\"value\":1},\"label\":\"before\"}");
    cJSON *shallow=cJSON_Duplicate(source,0), *deep=cJSON_Duplicate(source,1);
    assert(source && shallow && deep);
    assert(shallow->child==NULL && cJSON_IsObject(shallow));
    printf("shallow object=1 children=%d\n",cJSON_GetArraySize(shallow));
    cJSON *original_n=cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(source,"nested"),"value");
    cJSON *copied_n=cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(deep,"nested"),"value");
    assert(original_n!=copied_n && copied_n->valuedouble==1);
    cJSON_SetNumberValue(original_n,9);
    printf("deep after_source_mutation source=%.0f copy=%.0f distinct=%d\n",original_n->valuedouble,copied_n->valuedouble,original_n!=copied_n);
    assert(copied_n->valuedouble==1);
    char storage[]="borrowed";
    cJSON *reference=cJSON_CreateStringReference(storage);
    cJSON *owned_copy=cJSON_Duplicate(reference,1);
    assert(reference && owned_copy && !(owned_copy->type & cJSON_IsReference));
    storage[0]='B';assert(strcmp(owned_copy->valuestring,"borrowed")==0);
    printf("reference after_storage_mutation source=%s copy=%s copy_is_reference=%d\n",reference->valuestring,owned_copy->valuestring,!!(owned_copy->type & cJSON_IsReference));
    cJSON_Delete(source);cJSON_Delete(shallow);cJSON_Delete(deep);cJSON_Delete(reference);cJSON_Delete(owned_copy);
    return 0;
}
