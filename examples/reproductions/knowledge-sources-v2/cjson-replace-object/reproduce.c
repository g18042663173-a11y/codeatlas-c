/* CC0-1.0: key replacement and failed replacement ownership. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
int main(void) {
    cJSON *object=cJSON_Parse("{\"Key\":1,\"tail\":2}"), *replacement=cJSON_CreateNumber(8);
    assert(object && replacement);
    assert(cJSON_ReplaceItemInObjectCaseSensitive(object,"key",replacement)==0);
    assert(cJSON_GetObjectItemCaseSensitive(object,"Key")->valuedouble==1);
    printf("case_sensitive_missing replaced=0 original=%.0f replacement_detached=%d\n",object->child->valuedouble,replacement->next==NULL && replacement->prev==NULL);
    assert(cJSON_ReplaceItemInObject(object,"key",replacement)==1);
    assert(object->child==replacement && strcmp(replacement->string,"key")==0);
    assert(replacement->next->prev==replacement && object->child->prev==replacement->next);
    printf("case_insensitive replaced=1 key=%s value=%.0f next_key=%s list_links=valid\n",replacement->string,replacement->valuedouble,replacement->next->string);
    cJSON *unattached=cJSON_CreateNumber(3);assert(unattached);
    assert(cJSON_ReplaceItemInObject(object,"absent",unattached)==0);
    printf("missing_key replaced=0 caller_still_owns_value=%.0f\n",unattached->valuedouble);
    cJSON_Delete(unattached);cJSON_Delete(object);
    return 0;
}
