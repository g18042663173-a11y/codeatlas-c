#include "widget.hpp"

int via_base(Widget *p)
{
    return p->size();
}

int use_widget()
{
    Widget w;
    w.reset();
    Counter c;
    return helper(c.size()) + via_base(&c) + w.apply(helper, 2);
}

int run_all()
{
    return use_widget();
}
