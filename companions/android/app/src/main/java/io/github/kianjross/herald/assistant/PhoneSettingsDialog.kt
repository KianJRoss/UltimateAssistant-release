package io.github.kianjross.herald.assistant

import android.app.Dialog
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.Gravity
import android.view.View
import android.view.Window
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Conversation settings, using the same dark palette as the message surface. */
class PhoneSettingsDialog(
    context: Context,
    paired: Boolean,
    backgroundEnabled: Boolean,
    grants: Map<String, Boolean>,
    defaultAssistant: Boolean = false,
    private val onAction: (String) -> Unit
) : Dialog(context) {
    private fun dp(value: Int) = (value * context.resources.displayMetrics.density).toInt()
    private val foreground = Color.rgb(238,241,247)
    private val muted = Color.rgb(151,163,184)
    private fun shape(color: Int, radius: Int) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radius).toFloat()
    }
    private fun text(label: String, size: Float, color: Int = foreground) = TextView(context).apply {
        text = label; textSize = size; setTextColor(color)
    }
    init {
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        val panel = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20),dp(12),dp(20),dp(24))
            background = shape(Color.rgb(25,30,40),28)
        }
        panel.addView(View(context).apply { background = shape(Color.rgb(74,83,103),3) },
            LinearLayout.LayoutParams(dp(38),dp(4)).apply { gravity = Gravity.CENTER; bottomMargin=dp(18) })
        val heading = LinearLayout(context).apply { gravity=Gravity.CENTER_VERTICAL }
        heading.addView(text("Settings",24f).apply { setTypeface(typeface,Typeface.BOLD) },LinearLayout.LayoutParams(0,-2,1f))
        heading.addView(text("\u2715",20f).apply {
            gravity=Gravity.CENTER; contentDescription="Close settings"
            background=shape(Color.rgb(40,47,62),16)
            setOnClickListener { dismiss() }
        },LinearLayout.LayoutParams(dp(48),dp(48)))
        panel.addView(heading)
        panel.addView(text("Your phone, connected to your assistant",13f,muted).apply { setPadding(0,dp(4),0,dp(16)) })
        val content=LinearLayout(context).apply { orientation=LinearLayout.VERTICAL }
        val scroll=ScrollView(context).apply { isFillViewport=false; addView(content) }
        panel.addView(scroll,LinearLayout.LayoutParams(-1,0,1f))
        fun section(title: String): LinearLayout {
            content.addView(text(title.uppercase(),11f,muted).apply {
                letterSpacing=.12f; setTypeface(typeface,Typeface.BOLD); setPadding(dp(4),dp(16),0,dp(8))
            })
            val card=LinearLayout(context).apply {
                orientation=LinearLayout.VERTICAL
                background=shape(Color.rgb(32,38,51),20)
                clipToOutline=true
            }
            content.addView(card)
            return card
        }
        fun row(card: LinearLayout, title: String, detail: String, action: String, badge: String?=null, enabled: Boolean=false, danger: Boolean=false) {
            if(card.childCount>0) card.addView(View(context).apply { setBackgroundColor(Color.rgb(48,57,74)) },
                LinearLayout.LayoutParams(-1,dp(1)).apply { marginStart=dp(16); marginEnd=dp(16) })
            val line=LinearLayout(context).apply {
                orientation=LinearLayout.HORIZONTAL; gravity=Gravity.CENTER_VERTICAL
                minimumHeight=dp(72); setPadding(dp(16),dp(14),dp(14),dp(14))
                background=RippleDrawable(ColorStateList.valueOf(Color.argb(24,255,255,255)),null,null)
                isFocusable=true; setOnClickListener { dismiss(); onAction(action) }
            }
            val labels=LinearLayout(context).apply { orientation=LinearLayout.VERTICAL }
            labels.addView(text(title,15f,if(danger) Color.rgb(244,156,164) else foreground))
            labels.addView(text(detail,12f,muted).apply { setPadding(0,dp(4),0,0) })
            line.addView(labels,LinearLayout.LayoutParams(0,-2,1f))
            if(badge!=null) line.addView(text(badge,11f,if(enabled) Color.rgb(150,220,189) else muted).apply {
                setPadding(dp(9),dp(5),dp(9),dp(5))
                background=shape(if(enabled) Color.rgb(37,64,58) else Color.rgb(43,50,65),10)
            },LinearLayout.LayoutParams(-2,-2).apply { marginStart=dp(8) })
            line.addView(text("\u203a",22f,muted).apply { setPadding(dp(10),0,0,0) })
            card.addView(line)
        }
        val connection=section("Connection")
        row(connection,"Assistant",if(paired) "Paired through your private network" else "Connect through your private network","reconnect",if(paired) "Paired" else "Connect",paired)
        row(connection,"Background connection",if(backgroundEnabled) "Keep receiving when you leave the app" else "Tap to resume the background connection","background",if(backgroundEnabled) "On" else "Paused",backgroundEnabled)
        row(connection,"Sync now","Send pending updates and shared content","sync")
        row(connection,"Default digital assistant","Use your Android assistant gesture to open this conversation","default_assistant",if(defaultAssistant) "Selected" else "Choose",defaultAssistant)
        val permissionCard=section("Phone permissions")
        fun permission(title:String,detail:String,key:String) = row(permissionCard,title,detail,key,if(grants[key]==true) "Enabled" else "Set up",grants[key]==true)
        permission("Microphone","Dictate messages with your voice","microphone")
        permission("Notifications","Receive assistant updates","notifications")
        permission("Notification access","Let your assistant read phone notifications","notification_access")
        permission("Accessibility","Allow assistant actions on your phone","accessibility")
        permission("Shizuku control","Shell-level device and file actions; Android's protected private data remains restricted","privileged_control")
        val files=section("Files")
        row(files,"Upload files","Choose documents, images or study material","files")
        row(files,"Connect a folder","Grant ongoing access to a selected folder","folder")
        val advanced=section("Advanced")
        row(advanced,"Backend settings","Manage an alternate backend or invitation","backend")
        row(advanced,"Unpair phone","Remove this phone's assistant connection","unpair",danger=true)
        setContentView(panel)
        window?.apply {
            setBackgroundDrawableResource(android.R.color.transparent)
            addFlags(android.view.WindowManager.LayoutParams.FLAG_DIM_BEHIND)
            attributes=attributes.apply { dimAmount=.6f }
            setGravity(Gravity.BOTTOM)
        }
    }
    override fun onStart() {
        super.onStart()
        window?.setLayout(android.view.ViewGroup.LayoutParams.MATCH_PARENT,
            (context.resources.displayMetrics.heightPixels * .88f).toInt())
    }
}
