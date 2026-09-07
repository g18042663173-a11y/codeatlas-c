#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"

static char *nested_array(size_t depth)
{
    size_t index;
    char *text = malloc(depth * 2 + 2);
    if (text == NULL) {
        return NULL;
    }
    for (index = 0; index < depth; ++index) {
        text[index] = '[';
    }
    text[depth] = '0';
    for (index = 0; index < depth; ++index) {
        text[depth + 1 + index] = ']';
    }
    text[depth * 2 + 1] = '\0';
    return text;
}

static int run_case(size_t depth)
{
    const char *parse_end = NULL;
    char *input = nested_array(depth);
    cJSON *value;
    size_t offset;
    if (input == NULL) {
        return 2;
    }
    value = cJSON_ParseWithOpts(input, &parse_end, 1);
    offset = parse_end == NULL ? 0 : (size_t)(parse_end - input);
    printf("depth=%zu result=%s parse_end=%zu input_len=%zu\n",
           depth, value == NULL ? "NULL" : "ok", offset, strlen(input));
    cJSON_Delete(value);
    free(input);
    return 0;
}

int main(void)
{
    /*
     * These depths are deliberately fixed instead of being derived from the
     * library macro.  That makes a future CJSON_NESTING_LIMIT change observable
     * rather than silently moving both the treatment and the test inputs.
     */
    static const size_t fixed_depths[] = {999u, 1000u, 1001u};
    size_t index;
    int status = 0;
    for (index = 0; index < sizeof(fixed_depths) / sizeof(fixed_depths[0]); ++index) {
        status |= run_case(fixed_depths[index]);
    }
    return status;
}
