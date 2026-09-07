#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"

static char *nested_array(size_t depth)
{
    size_t index;
    char *text = malloc((depth * 2) + 2);
    if (text == NULL) {
        return NULL;
    }
    for (index = 0; index < depth; index++) {
        text[index] = '[';
    }
    text[depth] = '0';
    for (index = 0; index < depth; index++) {
        text[depth + 1 + index] = ']';
    }
    text[(depth * 2) + 1] = '\0';
    return text;
}

static void print_parse_status(const char *label, const char *text)
{
    cJSON *item = cJSON_Parse(text);
    printf("%s=%s\n", label, item == NULL ? "fail" : "pass");
    cJSON_Delete(item);
}

int main(void)
{
    cJSON *array_two = cJSON_Parse("[1,2]");
    cJSON *printed_object = cJSON_Parse("{\"a\":1,\"b\":2}");
    cJSON *number = cJSON_Parse("1");
    cJSON *lookup_object = cJSON_Parse("{\"key\":7}");
    char *nested_two = nested_array(2);
    char *nested_three = nested_array(3);
    char *nested_limit = nested_array(1000);
    char *nested_limit_plus_one = nested_array(1001);
    char *nested_limit_plus_two = nested_array(1002);
    char *printed = NULL;
    const char *failure_end = NULL;
    cJSON *deep_failure = NULL;
    cJSON *lookup = NULL;

    if ((printed_object == NULL) || (number == NULL) || (lookup_object == NULL)
        || (nested_two == NULL) || (nested_three == NULL)
        || (nested_limit == NULL) || (nested_limit_plus_one == NULL)
        || (nested_limit_plus_two == NULL)) {
        return 2;
    }
    printed = cJSON_PrintUnformatted(printed_object);
    if (printed == NULL) {
        return 3;
    }
    lookup = cJSON_GetObjectItem(lookup_object, "KEY");

    print_parse_status("simple_array", "[0]");
    print_parse_status("simple_object", "{\"value\":0}");
    print_parse_status("nested_2", nested_two);
    print_parse_status("nested_3", nested_three);
    print_parse_status("nested_limit", nested_limit);
    print_parse_status("nested_limit_plus_one", nested_limit_plus_one);
    print_parse_status("nested_limit_plus_two", nested_limit_plus_two);
    deep_failure = cJSON_ParseWithOpts(nested_limit_plus_two, &failure_end, 1);
    printf("deep_failure_offset=%ld\n",
           deep_failure == NULL ? (long)(failure_end - nested_limit_plus_two) : -1L);
    cJSON_Delete(deep_failure);

    printf("array_two=%d\n", cJSON_GetArraySize(array_two));
    printf("print_compact=%s\n", strcmp(printed, "{\"a\":1,\"b\":2}") == 0 ? "yes" : "no");
    printf("number=%.0f\n", cJSON_GetNumberValue(number));
    printf("lookup=%s\n", lookup == NULL ? "miss" : "found");
    printf("version=%s\n", cJSON_Version());

    cJSON_free(printed);
    cJSON_Delete(array_two);
    cJSON_Delete(printed_object);
    cJSON_Delete(number);
    cJSON_Delete(lookup_object);
    free(nested_two);
    free(nested_three);
    free(nested_limit);
    free(nested_limit_plus_one);
    free(nested_limit_plus_two);
    return 0;
}
